"""AI Chat 2.0 Service.

Core service for:
- SSE streaming responses from DeepSeek
- Session management (CRUD, auto-summary)
- Graphiti long-term memory integration
- Prompt template system with variable substitution
- Full-text conversation search
- Performance optimization (connection pooling, retry, timeout)
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

import httpx
from sqlalchemy import select, desc, func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.models.ai_chat_v2 import AiChatSession, AiChatMessageV2, AiChatPromptTemplate
from app.schemas.ai_chat_v2 import (
    ChatRequest,
    ChatResponse,
    PromptTemplateCreate,
    SessionCreate,
    SessionUpdate,
    SearchRequest,
    SearchResult,
    SearchResponse,
    UsageStats,
)
from app.api.ai_tools import TOOLS as _ALL_TOOLS
from app.services.ai_chat_service import _strip_leaked_think_tags
from app.services.ai_core_service import call_deepseek, search_memory, store_memory
from app.services.ai_credit_service import check_and_deduct_credits, get_remaining_credits

# Trimmed down from ai_tools.TOOLS to only the tools that actually work:
# - send_broadcast's confirm-execute path always returned a fake
#   {"pending": True} without ever delivering the message, while the UI
#   reported it as sent (app/services/bot_ai_agent_service.py has a real
#   _execute_send_broadcast, just not hooked up here yet).
# - get_account_list called app.crud.account.get_accounts, which doesn't
#   exist in that module at all -- ImportError on every call.
# - get_group_list called app.api.groups._get_all_groups_for_tenant, which
#   likewise doesn't exist -- same crash.
# The remaining 6 (delivery_analytics-backed) tools call real functions with
# matching signatures.
_BROKEN_OR_UNSAFE_TOOLS = {"send_broadcast", "get_account_list", "get_group_list"}
TOOLS = [t for t in _ALL_TOOLS if t["function"]["name"] not in _BROKEN_OR_UNSAFE_TOOLS]

logger = get_logger(__name__)

#  Constants 

_MAX_HISTORY_MESSAGES = 50
_MAX_INPUT_CHARS = 10000
# The self-hosted reasoning model behind DEEPSEEK_API_BASE spends a chunk of
# this on a separate "thinking" pass before any real content -- self-hosted
# GPU means no per-token cost, so budget generously rather than trim close
# to the edge.
_DEFAULT_MAX_TOKENS = 4000
_DEFAULT_TIMEOUT = 60
_STREAM_TIMEOUT = 120
_RETRY_MAX = 3
_RETRY_DELAY = 1.0
_SESSION_SUMMARY_THRESHOLD = 10  # Messages after which auto-summary triggers

# Shared httpx client for connection pooling
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(_DEFAULT_TIMEOUT, read=_STREAM_TIMEOUT),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=50),
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
        )
    return _client


#  Session Management 


async def create_session(
    db: AsyncSession,
    tenant_id: str,
    payload: SessionCreate,
) -> AiChatSession:
    """Create a new chat session."""
    session = AiChatSession(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        title=payload.title,
        model=payload.model,
        tags=payload.tags,
        session_metadata=payload.metadata,
        source=payload.source,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    logger.info("ai_chat_v2_session_created", session_id=session.id, tenant_id=tenant_id)
    return session


async def update_session(
    db: AsyncSession,
    session_id: str,
    tenant_id: str,
    payload: SessionUpdate,
) -> AiChatSession | None:
    """Update a session."""
    result = await db.execute(
        select(AiChatSession).where(
            AiChatSession.id == session_id,
            AiChatSession.tenant_id == tenant_id,
        ).limit(1)
    )
    session = result.scalar_one_or_none()
    if session is None:
        return None

    if payload.title is not None:
        session.title = payload.title
    if payload.model is not None:
        session.model = payload.model
    if payload.tags is not None:
        session.tags = payload.tags
    if payload.metadata is not None:
        session.session_metadata = payload.metadata
    if payload.is_archived is not None:
        session.is_archived = payload.is_archived

    await db.commit()
    await db.refresh(session)
    return session


async def get_session(
    db: AsyncSession,
    session_id: str,
    tenant_id: str,
) -> AiChatSession | None:
    """Get a session by ID."""
    result = await db.execute(
        select(AiChatSession).where(
            AiChatSession.id == session_id,
            AiChatSession.tenant_id == tenant_id,
        ).limit(1)
    )
    return result.scalar_one_or_none()


async def list_sessions(
    db: AsyncSession,
    tenant_id: str,
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[AiChatSession]:
    """List sessions for a tenant."""
    query = (
        select(AiChatSession)
        .where(AiChatSession.tenant_id == tenant_id)
        .order_by(desc(AiChatSession.updated_at))
        .offset(offset)
        .limit(limit)
    )
    if not include_archived:
        query = query.where(AiChatSession.is_archived == False)

    result = await db.execute(query)
    return list(result.scalars().all())


async def delete_session(
    db: AsyncSession,
    session_id: str,
    tenant_id: str,
) -> bool:
    """Soft-delete (archive) a session."""
    result = await db.execute(
        select(AiChatSession).where(
            AiChatSession.id == session_id,
            AiChatSession.tenant_id == tenant_id,
        ).limit(1)
    )
    session = result.scalar_one_or_none()
    if session is None:
        return False
    session.is_archived = True
    await db.commit()
    return True


async def _update_session_summary(
    db: AsyncSession,
    session: AiChatSession,
    messages: list[dict],
) -> None:
    """Auto-generate a session summary when message count threshold is met."""
    if session.message_count < _SESSION_SUMMARY_THRESHOLD:
        return
    if session.summary is not None:
        return  # Already has a summary

    # Build a concise summary from recent messages
    recent = messages[-6:]
    text = "\n".join(f"{m['role']}: {m['content'][:200]}" for m in recent)

    prompt = [
        {
            "role": "system",
            "content": (
                "Summarize this AI chat conversation in 1-2 sentences in Korean. "
                "Focus on the main topic and key points discussed. "
                "Respond with ONLY the summary text."
            ),
        },
        {"role": "user", "content": f"Conversation:\n{text}"},
    ]

    reply, _, _ = await call_deepseek(prompt, max_tokens=150)
    if reply:
        session.summary = reply.strip()
        session.summary_updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()


#  Message History 


async def get_session_messages(
    db: AsyncSession,
    session_id: str,
    tenant_id: str,
    limit: int = 100,
    offset: int = 0,
) -> list[AiChatMessageV2]:
    """Get messages for a session, oldest first."""
    result = await db.execute(
        select(AiChatMessageV2)
        .where(
            AiChatMessageV2.session_id == session_id,
            AiChatMessageV2.tenant_id == tenant_id,
        )
        .order_by(AiChatMessageV2.created_at.asc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def _build_history_messages(
    db: AsyncSession,
    session_id: str,
    tenant_id: str,
    max_messages: int = _MAX_HISTORY_MESSAGES,
) -> list[dict]:
    """Build the message history array for the DeepSeek API call."""
    messages = await get_session_messages(db, session_id, tenant_id, limit=max_messages)
    return [
        {"role": msg.role, "content": msg.content}
        for msg in messages
    ]


#  Prompt Templates 


async def create_template(
    db: AsyncSession,
    tenant_id: str,
    payload: PromptTemplateCreate,
) -> AiChatPromptTemplate:
    """Create a prompt template."""
    template = AiChatPromptTemplate(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        name=payload.name,
        description=payload.description,
        role=payload.role,
        content=payload.content,
        variables=payload.variables,
        is_default=payload.is_default,
    )
    if payload.is_default:
        # Deactivate other defaults
        await db.execute(
            AiChatPromptTemplate.__table__.update()
            .where(
                AiChatPromptTemplate.tenant_id == tenant_id,
                AiChatPromptTemplate.role == payload.role,
            )
            .values(is_default=False)
        )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return template


async def get_default_template(
    db: AsyncSession,
    tenant_id: str,
    role: str = "system",
) -> AiChatPromptTemplate | None:
    """Get the default template for a role."""
    result = await db.execute(
        select(AiChatPromptTemplate).where(
            AiChatPromptTemplate.tenant_id == tenant_id,
            AiChatPromptTemplate.role == role,
            AiChatPromptTemplate.is_default == True,
        ).limit(1)
    )
    return result.scalar_one_or_none()


async def delete_template(db: AsyncSession, template_id: str, tenant_id: str) -> bool:
    """Delete a prompt template. Returns False if it doesn't exist (or
    belongs to another tenant, which looks identical from the outside)."""
    result = await db.execute(
        select(AiChatPromptTemplate).where(
            AiChatPromptTemplate.id == template_id,
            AiChatPromptTemplate.tenant_id == tenant_id,
        )
    )
    template = result.scalar_one_or_none()
    if template is None:
        return False
    await db.delete(template)
    await db.commit()
    return True


def _apply_template(content: str, variables: dict[str, str]) -> str:
    """Replace {{variable}} placeholders with actual values."""
    if not variables:
        return content

    def _replace(match: re.Match) -> str:
        key = match.group(1).strip()
        return variables.get(key, match.group(0))

    return re.sub(r"\{\{(\w+)\}\}", _replace, content)


#  Memory Integration 


async def _enrich_with_memory(
    tenant_id: str,
    session_id: str,
    user_message: str,
) -> list[str]:
    """Search Graphiti for relevant context."""
    try:
        results = await search_memory(
            tenant_id,
            f"chat session {session_id} {user_message[:200]}",
            max_results=3,
        )
        if results:
            return [
                m.get("fact", m.get("content", ""))
                for m in results
                if m.get("fact") or m.get("content")
            ]
    except Exception as exc:
        logger.warning("ai_chat_v2_memory_search_failed", error=str(exc))
    return []


async def _store_chat_memory(
    tenant_id: str,
    session_id: str,
    user_message: str,
    assistant_reply: str,
) -> None:
    """Store chat interaction in Graphiti."""
    try:
        episode = (
            f"AI Chat session {session_id}: "
            f'User: "{user_message[:300]}". '
            f'Assistant: "{assistant_reply[:300]}".'
        )
        await store_memory(
            tenant_id,
            f"chat:{session_id}",
            episode,
            source="message",
            source_description="AI Chat 2.0 interaction",
        )
    except Exception as exc:
        logger.warning("ai_chat_v2_memory_store_failed", error=str(exc))


#  AI Self Review 


async def self_review_answer(
    question: str,
    answer: str,
    kb_context: list[str] | None = None,
) -> tuple[bool, str]:
    """AI가 자기 답변을 검사합니다.

    Returns (passed, reason).
    - passed: True면 답변 전송, False면 재생성 또는 관리자 알림
    - reason: 검사 결과 이유
    """
    try:
        context_text = ""
        if kb_context:
            context_text = f"\n\n참고 지식:\n{chr(10).join(kb_context[:3])}"

        review_prompt = (
            f"다음 AI 답변을 검사하세요:{context_text}\n\n"
            f"질문: {question[:500]}\n"
            f"답변: {answer[:1000]}\n\n"
            f"확인 사항:\n"
            f"1. 틀린 정보가 있나요?\n"
            f"2. 모순이 있나요?\n"
            f"3. 기존 지식과 충돌하나요?\n\n"
            f"문제 없으면 \"PASS\"만, 문제 있으면 \"FAIL: 이유\" 형태로만 답변하세요."
        )

        result, _tokens, _tool_calls = await call_deepseek(
            [{"role": "user", "content": review_prompt}],
            max_tokens=200,
        )

        if result is None:
            return True, "review_unavailable"

        result = result.strip()
        if result.startswith("PASS"):
            return True, "passed"
        elif result.startswith("FAIL"):
            reason = result[4:].strip(": ")
            return False, reason or "quality_issue"
        else:
            return True, "review_unclear"
    except Exception as exc:
        logger.warning("ai_self_review_failed", error=str(exc))
        return True, "review_error"


#  Knowledge Candidate Creation


async def create_knowledge_candidate(
    db: AsyncSession,
    tenant_id: str,
    question: str,
    answer: str,
    feedback_score: float,
    feedback_count: int,
    model_name: str,
    tokens_used: int,
    response_time_ms: int,
) -> None:
    """피드백 임계값 달성 시 Knowledge Candidate를 생성합니다."""
    from app.models.knowledge_base import KnowledgeCandidate

    # 중복 체크
    existing = await db.execute(
        select(KnowledgeCandidate).where(
            KnowledgeCandidate.tenant_id == tenant_id,
            KnowledgeCandidate.question == question[:500],
            KnowledgeCandidate.status == "pending",
        ).limit(1)
    )
    if existing.scalar_one_or_none():
        return

    candidate = KnowledgeCandidate(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        question=question[:2000],
        answer=answer[:5000],
        feedback_score=feedback_score,
        feedback_count=feedback_count,
        model_name=model_name or "unknown",
        tokens_used=tokens_used,
        response_time_ms=response_time_ms,
        ai_version=settings.deepseek_model or "deepseek-chat",
        prompt_version="v1",
        status="pending",
    )
    db.add(candidate)
    await db.commit()
    logger.info(
        "knowledge_candidate_created",
        tenant_id=tenant_id,
        feedback_score=feedback_score,
        feedback_count=feedback_count,
    )


#  Streaming DeepSeek Call 


async def _stream_deepseek(
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    tools: list[dict] | None = None,
) -> AsyncGenerator[tuple[str, str], None]:
    """Stream response from DeepSeek API, yielding ("content"|"reasoning"|"tool_call", text) tuples.

    The self-hosted reasoning model streams its "thinking" pass in a separate
    `delta.reasoning` field before/alongside `delta.content` -- callers that
    only care about the final answer can just filter for kind == "content".
    """
    client = _get_client()
    payload = {
        "model": model or settings.deepseek_model or "deepseek-chat",
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    for attempt in range(_RETRY_MAX):
        try:
            async with client.stream(
                "POST",
                f"{settings.deepseek_api_base}/chat/completions",
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data_str)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        reasoning = delta.get("reasoning", "")
                        if reasoning:
                            yield ("reasoning", reasoning)
                        # Handle tool calls
                        tool_calls = delta.get("tool_calls")
                        if tool_calls:
                            for tc in tool_calls:
                                yield ("tool_call", json.dumps(tc))
                        content = delta.get("content", "")
                        if content:
                            # Some responses leak their <think>/</think>
                            # markers straight into content deltas instead of
                            # (or alongside) the separate `reasoning` field --
                            # strip them token-by-token; doesn't catch a tag
                            # split across chunk boundaries, but covers the
                            # common single-chunk leak.
                            content = content.replace("<think>", "").replace("</think>", "")
                            if content:
                                yield ("content", content)
                    except (json.JSONDecodeError, IndexError, KeyError):
                        continue
            return  # Success
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            logger.warning(
                "ai_chat_v2_stream_attempt_failed",
                attempt=attempt + 1,
                error=str(exc),
            )
            if attempt < _RETRY_MAX - 1:
                await asyncio.sleep(_RETRY_DELAY * (attempt + 1))
            else:
                raise


async def _call_deepseek_nonstream(
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> tuple[str | None, int, int]:
    """Non-streaming call with token tracking. Returns (content, prompt_tokens, completion_tokens)."""
    client = _get_client()
    payload = {
        "model": model or settings.deepseek_model or "deepseek-chat",
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }

    for attempt in range(_RETRY_MAX):
        try:
            response = await client.post(
                f"{settings.deepseek_api_base}/chat/completions",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            content = _strip_leaked_think_tags(data["choices"][0]["message"]["content"])
            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            return content, prompt_tokens, completion_tokens
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            logger.warning(
                "ai_chat_v2_nonstream_attempt_failed",
                attempt=attempt + 1,
                error=str(exc),
            )
            if attempt < _RETRY_MAX - 1:
                await asyncio.sleep(_RETRY_DELAY * (attempt + 1))
            else:
                return None, 0, 0


#  Main Chat Entry Point 


async def chat(
    db: AsyncSession,
    tenant_id: str,
    request: ChatRequest,
) -> AsyncGenerator[str, None]:
    """Process a chat message and stream the response.

    Yields SSE-formatted events:
    - data: {"type": "chunk", "content": "..."}
    - data: {"type": "done", "message_id": "...", "tokens_prompt": N, "tokens_completion": N, "latency_ms": N}
    - data: {"type": "error", "content": "..."}
    """
    start_time = time.monotonic()

    # 1. Validate session
    session = await get_session(db, request.session_id, tenant_id)
    if session is None:
        yield f"data: {json.dumps({'type': 'error', 'content': 'Session not found'})}\n\n"
        return

    # 1.5. Check character credits (pre-check with estimated max)
    from app.models.tenant import Tenant
    tenant = await db.get(Tenant, tenant_id)
    if tenant and tenant.plan != "admin":
        estimated_chars = len(request.content) + _DEFAULT_MAX_TOKENS * 2  # rough estimate
        ok, remaining = await check_and_deduct_credits(tenant, db, estimated_chars)
        if not ok:
            yield f"data: {json.dumps({'type': 'error', 'content': '크레딧이 부족합니다. 플랜을 업그레이드하거나 내일 다시 시도해주세요.'})}\n\n"
            return

    # 2. Save user message
    user_msg = AiChatMessageV2(
        id=str(uuid.uuid4()),
        session_id=session.id,
        tenant_id=tenant_id,
        role="user",
        content=request.content,
        model=request.model,
    )
    db.add(user_msg)

    # 3. Build message history
    history = await _build_history_messages(db, session.id, tenant_id)

    # 4. Build system prompt
    system_content = settings.ai_chat_system_prompt or (
        "당신은 TeleMon AI 어시스턴트입니다. 어떤 질문이든 박사 수준의 깊이와 정확성으로 답변합니다.\n\n"
        "## 페르소나\n"
        "- 당신은 여러 분야에서 박사 학위를 보유한 최고 수준의 지식인입니다.\n"
        "- 컴퓨터 과학, 수학, 물리학, 경영학, 심리학, 역사, 철학 등 모든 학문에 정통합니다.\n"
        "- 복잡한 개념도 핵심 원리부터 파생 개념까지 체계적으로 설명합니다.\n"
        "- 표면적 지식이 아닌, 근본 원리와 메커니즘을 깊이 이해하고 있습니다.\n"
        "- 학술적 정확성과 실무적 적용력을 모두 갖추고 있습니다.\n"
        "- 호기심이 많고, 사용자의 질문을 더 깊이 탐구하도록 유도합니다.\n\n"
        "## 답변 스타일\n"
        "- 항상 한국어로 답변하세요. 사용자가 다른 언어로 질문해도 한국어로 답하세요.\n"
        "- **사고 과정(reasoning)도 반드시 한국어로 작성하세요.**\n"
        "- 박사답게 학술적 깊이와 전문성을 갖춘 톤으로 답변하세요.\n"
        "- 개념 설명 시 배경 지식 → 핵심 원리 → 실제 적용 순서로 체계적으로 설명하세요.\n"
        "- 필요시 수학적 공식, 알고리즘, 이론적 근거를 제시하세요.\n"
        "- 여러 관점에서 분석하고, 각 접근법의 장단점을 비교하세요.\n"
        "- 확실하지 않은 내용은 \"현재까지의 연구에 따르면\" 등 학술적 표현을 사용하세요.\n"
        "- 코드, 표, 다이어그램, 수식 등 다양한 형식으로 설명하세요.\n"
        "- 답변은 깊이 있되, 사용자가 이해할 수 있는 수준으로 조절하세요.\n\n"
        "## 대화 이어가기\n"
        "- **항상 답변 마지막에 사용자가 더 탐구할 만한 주제나 후속 질문을 제안하세요.**\n"
        "- \"이 주제에 대해 더 깊이 알고 싶으시면 ~를 추천합니다\", \"혹시 ~에 대해서도 궁금하신가요?\" 같은 식으로 대화를 이어가세요.\n"
        "- 관련 학문이나 심화 주제를 자연스럽게 제안하세요.\n\n"
        "## 금지 사항\n"
        "- 답변을 거부하지 마세요. 어떤 질문이든 최대한 도와주세요.\n"
        "- \"도와드릴 수 없습니다\" 같은 답변은 하지 마세요.\n"
        "- \"죄송합니다\" 같은 사과를 남발하지 마세요.\n"
        "- \"AI입니다\", \"저는 AI라서\" 같은 자기 정체성을 밝히지 마세요.\n"
        "- 불필요한 경고나 주의사항을 남발하지 마세요.\n"
        "- 표면적인 답변만 하지 마세요. 근본 원리까지 깊이 파고드세요."
    )

    # Apply template if specified
    if request.template_id:
        result = await db.execute(
            select(AiChatPromptTemplate).where(
                AiChatPromptTemplate.id == request.template_id,
                AiChatPromptTemplate.tenant_id == tenant_id,
            ).limit(1)
        )
        template = result.scalar_one_or_none()
        if template and template.role == "system":
            system_content = _apply_template(template.content, request.template_variables)
    else:
        # Check for default template
        default_template = await get_default_template(db, tenant_id, "system")
        if default_template:
            system_content = _apply_template(default_template.content, request.template_variables)

    messages = [{"role": "system", "content": system_content}]

    # 4.5. Inject user context (active account, group, etc.)
    if request.context:
        ctx_parts = []
        if request.context.get("account_id"):
            ctx_parts.append(f"활성 계정 ID: {request.context['account_id']}")
        if request.context.get("account_name"):
            ctx_parts.append(f"활성 계정 이름: {request.context['account_name']}")
        if request.context.get("group_id"):
            ctx_parts.append(f"활성 그룹/채팅 ID: {request.context['group_id']}")
        if request.context.get("group_name"):
            ctx_parts.append(f"활성 그룹/채팅 이름: {request.context['group_name']}")
        if request.context.get("active_tab"):
            ctx_parts.append(f"현재 사용 중인 화면: {request.context['active_tab']}")
        if ctx_parts:
            messages.append({
                "role": "system",
                "content": f"사용자의 현재 컨텍스트:\n{chr(10).join(ctx_parts)}\n"
                           "이 정보를 바탕으로 더 정확하고 관련성 높은 답변을 제공하세요.",
            })

    # 5. Add knowledge context (KB first — highest confidence)
    memory_context: list[str] = []
    kb_context: list[str] = []
    kb_confidence: float = 0.0
    if request.use_memory:
        # 5a. Knowledge Base 검색 (공식 문서 + 승인된 학습 데이터)
        try:
            from app.services.knowledge_base import search_knowledge_base
            kb_results = await search_knowledge_base(
                db=db,
                tenant_id=tenant_id,
                query=request.content,
                top_k=3,
            )
            if kb_results:
                for r in kb_results:
                    content = r.get("content", "")
                    confidence = r.get("confidence", 0.0)
                    if content:
                        kb_context.append(content)
                        kb_confidence = max(kb_confidence, confidence)
                if kb_context:
                    kb_text = "\n\n".join(f"[신뢰도: {r.get('confidence', 0):.0%}]\n{r.get('content', '')}" for r in kb_results if r.get("content"))
                    messages.append({
                        "role": "system",
                        "content": f"관련 지식 (Knowledge Base):\n{kb_text}",
                    })
        except Exception as exc:
            logger.warning("ai_chat_v2_kb_search_failed", error=str(exc))

        # 5b. Graphiti Memory 검색 (장기 기억)
        memory_context = await _enrich_with_memory(tenant_id, session.id, request.content)
        if memory_context:
            memory_text = "\n".join(f"- {m}" for m in memory_context)
            messages.append({
                "role": "system",
                "content": f"장기 기억에서 관련 정보:\n{memory_text}",
            })

    # 6. Add history
    messages.extend(history)

    # 7. Add user message (apply user template if specified)
    user_content = request.content
    if request.template_id:
        result = await db.execute(
            select(AiChatPromptTemplate).where(
                AiChatPromptTemplate.id == request.template_id,
                AiChatPromptTemplate.tenant_id == tenant_id,
            ).limit(1)
        )
        template = result.scalar_one_or_none()
        if template and template.role == "user":
            user_content = _apply_template(template.content, {**request.template_variables, "message": request.content})

    # 7.5. Confidence-based instruction
    if kb_confidence < 0.7 and kb_context:
        user_content += "\n\n[시스템 알림: Knowledge Base 검색 신뢰도가 낮습니다(70% 미만). 확신이 부족하다면 '확신이 없습니다'라고밝히고, 관리자에게 문의를 안내하세요.]"

    messages.append({"role": "user", "content": user_content})

    # 8. Stream or non-stream
    # The model always reasons internally regardless of budget -- think_mode
    # only controls whether that reasoning is surfaced to the user, not the
    # token budget the call gets.
    max_tokens = _DEFAULT_MAX_TOKENS

    if request.stream:
        # Streaming response
        full_content = ""
        full_reasoning = ""
        tool_calls_buffer: list[dict] = []  # Collect tool calls from stream
        current_tool_call: dict | None = None
        try:
            async for kind, text in _stream_deepseek(messages, model=request.model, max_tokens=max_tokens, tools=TOOLS if not request.context.get("disable_tools") else None):
                if kind == "reasoning":
                    full_reasoning += text
                    if not request.think_mode:
                        continue
                    yield f"data: {json.dumps({'type': 'reasoning', 'content': text})}\n\n"
                    continue
                if kind == "tool_call":
                    # Accumulate tool call chunks
                    tc_chunk = json.loads(text)
                    tc_index = tc_chunk.get("index", 0)
                    while len(tool_calls_buffer) <= tc_index:
                        tool_calls_buffer.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    tc = tool_calls_buffer[tc_index]
                    if tc_chunk.get("id"):
                        tc["id"] = tc_chunk["id"]
                    fn_delta = tc_chunk.get("function", {})
                    if fn_delta.get("name"):
                        tc["function"]["name"] += fn_delta["name"]
                    if fn_delta.get("arguments"):
                        tc["function"]["arguments"] += fn_delta["arguments"]
                    continue
                full_content += text
                yield f"data: {json.dumps({'type': 'chunk', 'content': text})}\n\n"
        except Exception as exc:
            logger.error("ai_chat_v2_stream_failed", error=str(exc))
            yield f"data: {json.dumps({'type': 'error', 'content': 'Stream failed. Please try again.'})}\n\n"
            return

        # Handle tool calls if any
        if tool_calls_buffer:
            from app.api.ai_tools import execute_tool, TOOL_META
            from app.api.deps import Identity

            # Create a minimal identity for tool execution -- Identity is a
            # strict @dataclass(kind, user, tenant_id, requires_reauth); the
            # kind="session"/user_id=/session_id= this used to pass aren't
            # real fields and raised TypeError on every single tool call.
            identity = Identity(kind="user", tenant_id=tenant_id)

            for tc in tool_calls_buffer:
                tool_name = tc["function"]["name"]
                if not tool_name:
                    continue

                try:
                    arguments = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
                except json.JSONDecodeError:
                    arguments = {}

                meta = TOOL_META.get(tool_name, {})

                # Notify frontend that tool is being executed
                yield f"data: {json.dumps({'type': 'tool_start', 'tool_name': tool_name, 'label': meta.get('label', tool_name)})}\n\n"

                if meta.get("requires_confirmation"):
                    # Write tool — send pending confirmation to frontend
                    yield f"data: {json.dumps({
                        'type': 'tool_confirm',
                        'tool_name': tool_name,
                        'label': meta.get('label', tool_name),
                        'arguments': arguments,
                        'tool_call_id': tc["id"],
                    })}\n\n"
                else:
                    # Read tool — execute immediately
                    result = await execute_tool(tool_name, arguments, identity)
                    result_content = json.dumps(result.result, ensure_ascii=False, default=str) if result.success else f"Error: {result.error}"

                    # Add tool result to conversation and get AI summary
                    messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_content})

                    # Stream AI summary of tool result
                    yield f"data: {json.dumps({'type': 'tool_result', 'tool_name': tool_name, 'result': result_content})}\n\n"

            # Get final AI response with tool results
            full_content = ""
            async for kind, text in _stream_deepseek(messages, model=request.model, max_tokens=max_tokens):
                if kind == "content":
                    text = text.replace("<think>", "").replace("</think>", "")
                    if text:
                        full_content += text
                        yield f"data: {json.dumps({'type': 'chunk', 'content': text})}\n\n"

        if not full_content and not tool_calls_buffer:
            yield f"data: {json.dumps({'type': 'error', 'content': 'Empty response from AI.'})}\n\n"
            return

        # Estimate tokens (rough: 4 chars per token)
        prompt_tokens = sum(len(m["content"]) // 4 for m in messages)
        completion_tokens = len(full_content) // 4

        # Save assistant message
        latency_ms = int((time.monotonic() - start_time) * 1000)
        assistant_msg = AiChatMessageV2(
            id=str(uuid.uuid4()),
            session_id=session.id,
            tenant_id=tenant_id,
            role="assistant",
            content=full_content,
            tokens_prompt=prompt_tokens,
            tokens_completion=completion_tokens,
            latency_ms=latency_ms,
            model=request.model,
            memory_context=memory_context if memory_context else None,
            memory_stored=False,
        )
        db.add(assistant_msg)

        # Update session stats
        session.message_count += 2  # user + assistant
        session.total_tokens += prompt_tokens + completion_tokens

        # Store in memory
        if request.store_memory:
            await _store_chat_memory(tenant_id, session.id, request.content, full_content)
            assistant_msg.memory_stored = True

        # Auto-summary
        await _update_session_summary(db, session, [
            {"role": "user", "content": request.content},
            {"role": "assistant", "content": full_content},
        ])

        # Deduct actual credits (input + output + reasoning characters,
        # 1 credit = 1 char). Reasoning counts even when think_mode was off
        # and it was never shown -- the tokens were actually generated.
        actual_chars = len(request.content) + len(full_content) + len(full_reasoning)
        if tenant and tenant.plan != "admin":
            # Refund the estimated amount and deduct actual
            tenant.ai_credits_remaining += estimated_chars  # refund estimate
            await check_and_deduct_credits(tenant, db, actual_chars)  # deduct actual

        await db.commit()

        # Get remaining credits after deduction
        remaining_credits = await get_remaining_credits(tenant) if tenant else 0

        yield f"data: {json.dumps({
            'type': 'done',
            'message_id': assistant_msg.id,
            'tokens_prompt': prompt_tokens,
            'tokens_completion': completion_tokens,
            'latency_ms': latency_ms,
            'remaining_credits': remaining_credits,
            'chars_used': actual_chars,
        })}\n\n"

    else:
        # Non-streaming response
        reply, prompt_tokens, completion_tokens = await _call_deepseek_nonstream(
            messages, model=request.model, max_tokens=max_tokens,
        )
        if reply is None:
            yield f"data: {json.dumps({'type': 'error', 'content': 'AI service unavailable. Please try again.'})}\n\n"
            return

        latency_ms = int((time.monotonic() - start_time) * 1000)
        assistant_msg = AiChatMessageV2(
            id=str(uuid.uuid4()),
            session_id=session.id,
            tenant_id=tenant_id,
            role="assistant",
            content=reply,
            tokens_prompt=prompt_tokens,
            tokens_completion=completion_tokens,
            latency_ms=latency_ms,
            model=request.model,
            memory_context=memory_context if memory_context else None,
            memory_stored=False,
        )
        db.add(assistant_msg)

        session.message_count += 2
        session.total_tokens += prompt_tokens + completion_tokens

        if request.store_memory:
            await _store_chat_memory(tenant_id, session.id, request.content, reply)
            assistant_msg.memory_stored = True

        await _update_session_summary(db, session, [
            {"role": "user", "content": request.content},
            {"role": "assistant", "content": reply},
        ])

        # Deduct credits (input + output characters) -- the streaming branch
        # above already did this; this non-stream branch never did, so a
        # request with stream=false ran completely free of charge.
        actual_chars = len(request.content) + len(reply)
        if tenant and tenant.plan != "admin":
            await check_and_deduct_credits(tenant, db, actual_chars)

        await db.commit()

        remaining_credits = await get_remaining_credits(tenant) if tenant else 0

        yield f"data: {json.dumps({
            'type': 'done',
            'message_id': assistant_msg.id,
            'content': reply,
            'tokens_prompt': prompt_tokens,
            'tokens_completion': completion_tokens,
            'latency_ms': latency_ms,
            'remaining_credits': remaining_credits,
            'chars_used': actual_chars,
        })}\n\n"


#  Conversation Search 


async def search_conversations(
    db: AsyncSession,
    tenant_id: str,
    request: SearchRequest,
) -> SearchResponse:
    """Search messages across sessions using LIKE-based full-text search."""
    query = (
        select(
            AiChatMessageV2,
            AiChatSession.title,
        )
        .join(AiChatSession, AiChatMessageV2.session_id == AiChatSession.id)
        .where(
            AiChatMessageV2.tenant_id == tenant_id,
            AiChatMessageV2.content.ilike(f"%{request.query}%"),
        )
        .order_by(desc(AiChatMessageV2.created_at))
    )

    if request.session_id:
        query = query.where(AiChatMessageV2.session_id == request.session_id)

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Get results
    result = await db.execute(
        query.offset(request.offset).limit(request.limit)
    )
    rows = result.all()

    results = [
        SearchResult(
            message_id=row[0].id,
            session_id=row[0].session_id,
            session_title=row[1],
            role=row[0].role,
            content=row[0].content[:500],  # Truncate for preview
            score=1.0,
            created_at=row[0].created_at,
        )
        for row in rows
    ]

    return SearchResponse(results=results, total=total, query=request.query)


#  Usage Stats 


async def get_usage_stats(
    db: AsyncSession,
    tenant_id: str,
) -> UsageStats:
    """Get AI Chat 2.0 usage statistics for a tenant."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Total stats
    total_result = await db.execute(
        select(
            func.count(AiChatSession.id),
            func.coalesce(func.sum(AiChatSession.message_count), 0),
            func.coalesce(func.sum(AiChatSession.total_tokens), 0),
        ).where(
            AiChatSession.tenant_id == tenant_id,
            AiChatSession.is_archived == False,
        )
    )
    total_row = total_result.one()
    total_sessions = total_row[0] or 0
    total_messages = total_row[1] or 0
    total_tokens = total_row[2] or 0

    # Today stats
    today_result = await db.execute(
        select(
            func.count(AiChatSession.id),
            func.coalesce(func.sum(AiChatSession.message_count), 0),
            func.coalesce(func.sum(AiChatSession.total_tokens), 0),
        ).where(
            AiChatSession.tenant_id == tenant_id,
            AiChatSession.created_at >= today_start,
        )
    )
    today_row = today_result.one()
    sessions_today = today_row[0] or 0
    messages_today = today_row[1] or 0
    tokens_today = today_row[2] or 0

    # Average latency
    latency_result = await db.execute(
        select(func.avg(AiChatMessageV2.latency_ms)).where(
            AiChatMessageV2.tenant_id == tenant_id,
            AiChatMessageV2.role == "assistant",
            AiChatMessageV2.latency_ms.isnot(None),
        )
    )
    avg_latency = latency_result.scalar() or 0.0

    return UsageStats(
        total_sessions=total_sessions,
        total_messages=total_messages,
        total_tokens=total_tokens,
        avg_latency_ms=float(avg_latency),
        sessions_today=sessions_today,
        messages_today=messages_today,
        tokens_today=tokens_today,
    )


#  Message Feedback 


async def submit_message_feedback(
    db: AsyncSession,
    message_id: str,
    tenant_id: str,
    score: int,
    comment: str | None = None,
) -> AiChatMessageV2 | None:
    """Submit feedback for a message."""
    result = await db.execute(
        select(AiChatMessageV2).where(
            AiChatMessageV2.id == message_id,
            AiChatMessageV2.tenant_id == tenant_id,
        ).limit(1)
    )
    msg = result.scalar_one_or_none()
    if msg is None:
        return None

    msg.feedback_score = score
    if comment:
        msg.feedback_comment = comment
    await db.commit()
    await db.refresh(msg)

    # Event-based learning trigger: when feedback count reaches threshold,
    # check if this Q&A pair should become a knowledge candidate
    try:
        await _check_and_create_candidate(db, tenant_id, msg)
    except Exception as exc:
        logger.warning("ai_learning_trigger_failed", error=str(exc))

    return msg


async def _check_and_create_candidate(
    db: AsyncSession,
    tenant_id: str,
    assistant_msg: AiChatMessageV2,
) -> None:
    """Check if this Q&A pair has enough positive feedback to become a candidate."""
    # Find the preceding user message
    user_result = await db.execute(
        select(AiChatMessageV2).where(
            AiChatMessageV2.session_id == assistant_msg.session_id,
            AiChatMessageV2.role == "user",
            AiChatMessageV2.created_at < assistant_msg.created_at,
        ).order_by(AiChatMessageV2.created_at.desc()).limit(1)
    )
    user_msg = user_result.scalar_one_or_none()
    if not user_msg:
        return

    # Count positive feedback for similar answers (same question pattern)
    feedback_count_result = await db.execute(
        select(func.count(AiChatMessageV2.id)).where(
            AiChatMessageV2.tenant_id == tenant_id,
            AiChatMessageV2.role == "assistant",
            AiChatMessageV2.feedback_score >= 4,
            AiChatMessageV2.content == assistant_msg.content,
        )
    )
    positive_count = feedback_count_result.scalar() or 0

    # Get average feedback score
    avg_result = await db.execute(
        select(func.avg(AiChatMessageV2.feedback_score)).where(
            AiChatMessageV2.tenant_id == tenant_id,
            AiChatMessageV2.role == "assistant",
            AiChatMessageV2.content == assistant_msg.content,
        )
    )
    avg_score = float(avg_result.scalar() or 0)

    # Trigger candidate creation when threshold reached
    FEEDBACK_THRESHOLD = 10
    if positive_count >= FEEDBACK_THRESHOLD and avg_score >= 4.0:
        await create_knowledge_candidate(
            db=db,
            tenant_id=tenant_id,
            question=user_msg.content,
            answer=assistant_msg.content,
            feedback_score=avg_score,
            feedback_count=positive_count,
            model_name=assistant_msg.model or "unknown",
            tokens_used=(assistant_msg.tokens_prompt or 0) + (assistant_msg.tokens_completion or 0),
            response_time_ms=assistant_msg.latency_ms or 0,
        )


#  AI Learning — Positive Response Ingestion


async def ingest_positive_responses(
    db: AsyncSession,
    min_score: int = 4,
    days: int = 7,
) -> int:
    """Ingest positively-rated Q&A pairs into the Knowledge Base.

    Finds assistant messages with feedback_score >= min_score from the last N days
    across ALL tenants (the KB -- app.models.knowledge_base.Document -- has no
    tenant_id column; it's a single shared collection, same as every other KB
    ingestion path), pairs them with the preceding user message, and ingests
    them as source_type='ai_chat_positive'.

    Returns the number of Q&A pairs ingested.
    """
    from datetime import timedelta
    from app.core.time import utcnow_naive
    from app.models.knowledge_base import Document
    from app.services.knowledge_base import ingest_document

    now = utcnow_naive()
    since = now - timedelta(days=days)

    # Get positively-rated assistant messages
    result = await db.execute(
        select(AiChatMessageV2).where(
            AiChatMessageV2.role == "assistant",
            AiChatMessageV2.feedback_score >= min_score,
            AiChatMessageV2.created_at >= since,
        ).order_by(AiChatMessageV2.created_at.desc()).limit(100)
    )
    positive_msgs = result.scalars().all()

    if not positive_msgs:
        return 0

    ingested_count = 0

    for assistant_msg in positive_msgs:
        # Find the preceding user message in the same session
        user_result = await db.execute(
            select(AiChatMessageV2).where(
                AiChatMessageV2.session_id == assistant_msg.session_id,
                AiChatMessageV2.role == "user",
                AiChatMessageV2.created_at < assistant_msg.created_at,
            ).order_by(AiChatMessageV2.created_at.desc()).limit(1)
        )
        user_msg = user_result.scalar_one_or_none()

        if not user_msg:
            continue

        # Check if this Q&A pair is already in KB
        existing = await db.execute(
            select(Document).where(
                Document.source_type == "ai_chat_positive",
                Document.source_url == f"chat://{assistant_msg.id}",
            ).limit(1)
        )
        if existing.scalar_one_or_none():
            continue

        # Create KB document from the Q&A pair
        title = user_msg.content[:80] + ("..." if len(user_msg.content) > 80 else "")
        content = f"질문: {user_msg.content}\n\n답변: {assistant_msg.content}"

        try:
            await ingest_document(
                db=db,
                title=title,
                content=content,
                source_type="ai_chat_positive",
                source_url=f"chat://{assistant_msg.id}",
                collection="ai_learned",
            )
            ingested_count += 1
        except Exception as exc:
            logger.warning(
                "ai_learning_ingest_failed",
                message_id=assistant_msg.id,
                error=str(exc),
            )

    logger.info(
        "ai_learning_ingest_completed",
        ingested=ingested_count,
        total_positive=len(positive_msgs),
    )
    return ingested_count