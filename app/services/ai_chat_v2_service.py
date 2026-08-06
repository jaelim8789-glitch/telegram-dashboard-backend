"""AI Chat 2.0 Service.

Core service for:
- SSE streaming responses from Ollama
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
from app.services.ai_chat_service import _strip_leaked_think_tags, extract_confidence, CONFIDENCE_STREAM_HOLDBACK
from app.services.ai_core_service import call_ollama, search_memory, store_memory
from app.services.ai_credit_service import check_and_deduct_credits, get_remaining_credits
from app.services.ai_think_mode_heuristics import should_skip_think_mode

# Trimmed down from ai_tools.TOOLS to only the tools that actually work:
# - get_account_list called app.crud.account.get_accounts, which doesn't
#   exist in that module at all -- ImportError on every call.
# - get_group_list called app.api.groups._get_all_groups_for_tenant, which
#   likewise doesn't exist -- same crash.
# send_broadcast used to be excluded here too (its confirm-execute path
# always returned a fake {"pending": True} without ever delivering the
# message), but execute_tool() now calls the real
# bot_ai_agent_service._execute_send_broadcast, and the /confirm-tool
# endpoint is the only place that ever invokes it for a write tool -- so
# it's safe to expose again.
_BROKEN_OR_UNSAFE_TOOLS = {"get_account_list", "get_group_list"}
TOOLS = [t for t in _ALL_TOOLS if t["function"]["name"] not in _BROKEN_OR_UNSAFE_TOOLS]

logger = get_logger(__name__)

#  Constants 

_MAX_HISTORY_MESSAGES = 12
# When a session has more history than _MAX_HISTORY_MESSAGES, the older tail
# is condensed into this many "earlier conversation summary" lines instead of
# being passed verbatim — keeps context from overflowing (which makes the
# model answer tersely to "save" tokens).
_MAX_SUMMARY_LINES = 3

# Per-session summary cache — the expensive history-condensing pass runs at
# most once per session, not on every reply (huge mobile-latency win).
_session_summary_cache: dict[str, str] = {}
_MAX_INPUT_CHARS = 10000
# The self-hosted reasoning model behind OLLAMA_API_BASE spends a chunk of
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
            headers={"Authorization": f"Bearer {settings.ollama_api_key}"},
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
    if payload.is_pinned is not None:
        session.is_pinned = payload.is_pinned

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
        .order_by(desc(AiChatSession.is_pinned), desc(AiChatSession.updated_at))
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

    reply, _, _ = await call_ollama(prompt, max_tokens=150)
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


async def copy_message(
    db: AsyncSession,
    session_id: str,
    tenant_id: str,
    role: str,
    content: str,
    model: str,
) -> AiChatMessageV2 | None:
    """Insert a message verbatim into a session (used for branching an
    existing conversation into a new session). Not an AI call -- no
    generation, no credit deduction, just copying already-paid-for content.
    Returns None if the session doesn't belong to this tenant.
    """
    session = await get_session(db, session_id, tenant_id)
    if session is None:
        return None
    msg = AiChatMessageV2(
        id=str(uuid.uuid4()),
        session_id=session_id,
        tenant_id=tenant_id,
        role=role,
        content=content,
        model=model,
    )
    db.add(msg)
    session.message_count += 1
    await db.commit()
    await db.refresh(msg)
    return msg


async def _build_history_messages(
    db: AsyncSession,
    session_id: str,
    tenant_id: str,
    max_messages: int = _MAX_HISTORY_MESSAGES,
) -> list[dict]:
    """Build the message history array for the AI call.

    Keeps only the most recent *max_messages*. If the session is longer, the
    older tail is condensed into a short summary system line (best-effort) so
    context stays lean and the model answers fully instead of tersely.
    """
    all_msgs = await get_session_messages(db, session_id, tenant_id, limit=200)
    if not all_msgs:
        return []

    recent = all_msgs[-max_messages:]
    older = all_msgs[:-max_messages] if len(all_msgs) > max_messages else []

    history: list[dict] = []

    # Latency: these two summarization passes each cost an extra model call.
    # Run them only ONCE per session (cached) and only when there's enough
    # history to summarize — otherwise every reply pays the tax and mobile
    # users feel it as "AI is too slow".
    if older and _session_summary_cache.get(session_id) is None:
        summary = await _condense_history(older)
        _session_summary_cache[session_id] = summary or ""
        if summary:
            history.append({"role": "system", "content": f"이전 대화 요약:\n{summary}"})

    # D2: user interests — only compute early in the session (first 3 turns);
    # after that recent messages themselves carry the context.
    if len(all_msgs) <= 6:
        interests = await _extract_user_interests(recent)
        if interests:
            history.insert(0, {"role": "system", "content": f"사용자의 이번 세션 관심사:\n{interests}"})

    history.extend(
        {"role": msg.role, "content": msg.content}
        for msg in recent
        if msg.role in ("user", "assistant") and msg.content
    )
    return history


async def _extract_user_interests(recent: list[AiChatMessageV2]) -> str:
    """Best-effort: distill user questions into 1-2 interest lines."""
    user_msgs = [m.content for m in recent if m.role == "user" and m.content]
    if not user_msgs:
        return ""
    try:
        joined = "\n".join(f"- {u[:120]}" for u in user_msgs[-6:])
        result = await call_ollama(
            [{
                "role": "user",
                "content": (
                    "사용자가 이번 대화에서 계속 물어보는 주제/관심사를 "
                    "한국어 2줄 이내로 요약하세요. 추측하지 말고, 확실히 "
                    "드러난 것만.\n\n" + joined
                ),
            }],
            max_tokens=150,
        )
        out, _, _ = result if result else (None, 0, 0)
        return out.strip()[:400] if out and out.strip() else ""
    except Exception as exc:
        logger.debug("ai_interests_extract_failed", error=str(exc))
        return ""


async def _condense_history(older: list[AiChatMessageV2]) -> str:
    """Best-effort: summarize an older message tail into a few lines."""
    if not older:
        return ""
    try:
        text = "\n".join(
            f"{'사용자' if m.role == 'user' else 'AI'}: {m.content[:200]}" for m in older[-20:]
        )
        result = await call_ollama(
            [{
                "role": "user",
                "content": (
                    "아래 대화 내용을 한국어로 3줄 이내로 간결하게 요약해주세요. "
                    "핵심 주제, 사용자가 원했던 것, 결정된 사항만 담으세요.\n\n" + text
                ),
            }],
            max_tokens=200,
        )
        summary, _, _ = result if result else (None, 0, 0)
        if summary and summary.strip():
            return summary.strip()[:600]
    except Exception as exc:
        logger.debug("ai_history_condense_failed", error=str(exc))
    return ""


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


_COMPLEX_HINTS = (
    "어떻게", "방법", "분석", "비교", "차이", "코드", "코딩", "구현",
    "전략", "계획", "최적", "예시", "가이드", "튜토리얼", "작성해", "만들어",
    "원리", "이유", "설명", "리포트", "정리", "스크립트", "함수",
)
_SIMPLE_HINTS = (
    "안녕", "반가", "네", "고마", "응", "ㅇㅇ", "그래", "ㅋ", "하이", "hello", "hi",
    "좋아", "확인", "감사", "?",
)


def _classify_intent(text: str) -> str:
    """Rough intent classification: 'simple' | 'complex' | 'standard'.

    Drives dynamic token budget and auto think-mode. Pure heuristic — cheap,
    no extra LLM call.
    """
    t = (text or "").strip().lower()
    if not t or len(t) <= 4:
        return "simple"
    if any(h in t for h in _SIMPLE_HINTS) and len(t) <= 12:
        return "simple"
    if any(h in t for h in _COMPLEX_HINTS):
        return "complex"
    if len(t) > 120:
        return "complex"
    return "standard"


def _assistant_context(memory_context: list[str] | None, kb_context: list[str] | None) -> list | None:
    """Stores memory + RAG usage metadata on the assistant message so we can
    later measure whether KB-referenced answers actually get better feedback
    than non-referenced ones (RAG effectiveness).
    """
    ctx = list(memory_context) if memory_context else []
    base = {"used_rag": bool(kb_context), "rag_sources": len(kb_context) if kb_context else 0}
    return ctx + [base] if ctx else [base]


#  AI Self Review 


def _is_rambling(text: str) -> bool:
    """Heuristic: detects repetitive / looping answers (common with the
    abliterated 14B when repeat_penalty was off). Cheap, no extra LLM call.

    Returns True if the same sentence fragment repeats multiple times or a
    single n-gram dominates the reply.
    """
    if not text or len(text) < 60:
        return False
    # Split into rough sentence fragments (by . ! ? \n)
    frags = [f.strip() for f in re.split(r'[.!?\n]', text) if len(f.strip()) >= 8]
    if len(frags) < 4:
        return False
    seen: dict[str, int] = {}
    for f in frags:
        # Use a distinctive n-gram key: first ~12 chars (short enough to catch
        # repeated short sentences, long enough to avoid false positives).
        key = f[:12]
        seen[key] = seen.get(key, 0) + 1
    # Count how many fragments are "extra" repeats beyond the first occurrence.
    # e.g. 7× same sentence → 6 extra out of 7 → 0.86 → rambling.
    extras = sum(c - 1 for c in seen.values())
    if len(frags) == 0:
        return False
    return extras / len(frags) > 0.3


_REFUSAL_HINTS = (
    "죄송합니다", "죄송해요", "도와드릴 수 없", "도와줄 수 없", "하지 않겠",
    "할 수 없습니다", "안 됩니다", "불가능", "거부", "할 수 없어요",
    "제 역할이 아닙니다", "답변할 수 없", "could not help", "can't help",
)


def _is_refusal(text: str) -> bool:
    """Heuristic: detects refusal / dead-end answers that should be retried.
    The system prompt forbids refusing any question, so a refusal usually means
    the model fell into a refusal pattern — regenerate it.
    """
    if not text:
        return False
    low = text.lower()
    if len(text) < 80:
        for hint in _REFUSAL_HINTS:
            if hint in low:
                return True
    return False


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

        result, _tokens, _tool_calls = await call_ollama(
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
        ai_version=settings.ollama_model or "ollama-chat",
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


#  Streaming Ollama Call 


async def _stream_ollama(
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    tools: list[dict] | None = None,
    temperature: float = 0.7,
) -> AsyncGenerator[tuple[str, str], None]:
    """Stream response from Ollama API, yielding ("content"|"reasoning"|"tool_call", text) tuples.

    The self-hosted reasoning model streams its "thinking" pass in a separate
    `delta.reasoning` field before/alongside `delta.content` -- callers that
    only care about the final answer can just filter for kind == "content".
    """
    client = _get_client()
    payload = {
        "model": model or settings.ollama_model or "ollama-chat",
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": True,
        # Keep answers detailed & varied. The abliterated local model defaults
        # pushes longer, richer replies. `stop` prevents premature end-of-turn.
        # seed/top_k/repeat_penalty must be TOP-LEVEL for Ollama's OpenAI
        # compatible endpoint (0.32 verified) — the nested "options" object is
        # ignored there. The model's own default is repeat_penalty=1 (no
        # repetition suppression), which is why answers ramble; 1.1 fixes it.
        # Fixed seed gives deterministic output for regression testing.
        "temperature": temperature,
        "top_p": 0.9,
        "frequency_penalty": 0.3,
        "presence_penalty": 0.3,
        "stop": ["\n\n\n", "Human:", "사용자:"],
        "seed": 42,
        "top_k": 40,
        "repeat_penalty": 1.1,
        "num_ctx": 8192,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    for attempt in range(_RETRY_MAX):
        try:
            async with client.stream(
                "POST",
                f"{settings.ollama_api_base}/chat/completions",
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


async def _call_ollama_nonstream(
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = 0.7,
) -> tuple[str | None, int, int]:
    """Non-streaming call with token tracking. Returns (content, prompt_tokens, completion_tokens)."""
    client = _get_client()
    payload = {
        "model": model or settings.ollama_model or "ollama-chat",
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
        "temperature": temperature,
        "top_p": 0.9,
        "frequency_penalty": 0.3,
        "presence_penalty": 0.3,
        "stop": ["\n\n\n", "Human:", "사용자:"],
        "seed": 42,
        "top_k": 40,
        "repeat_penalty": 1.1,
        "num_ctx": 8192,
    }

    for attempt in range(_RETRY_MAX):
        try:
            response = await client.post(
                f"{settings.ollama_api_base}/chat/completions",
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

    # 1.4. Short greeting/ack messages never need a reasoning pass -- force
    # think_mode off regardless of the frontend toggle to skip the
    # reasoning-stream overhead. Logged with before/after so the latency
    # improvement can be measured post-deploy.
    if request.think_mode and should_skip_think_mode(request.content):
        logger.info(
            "ai_chat_v2_think_mode_auto_skipped",
            tenant_id=tenant_id,
            requested_think_mode=True,
            effective_think_mode=False,
            content_length=len(request.content),
        )
        request.think_mode = False

    # 1.5. Check character credits (pre-check with estimated max)
    # Pre-check uses a realistic upper bound (question + ~2000 chars of answer)
    # instead of _DEFAULT_MAX_TOKENS*2 (8000+ chars) — the old estimate blocked
    # users with 5000 credits because the estimate was far larger than any real
    # answer. Actual usage is still deducted accurately after generation.
    from app.models.tenant import Tenant
    tenant = await db.get(Tenant, tenant_id)
    if tenant and tenant.plan != "admin":
        estimated_chars = len(request.content) + 2000
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
        "당신은 TeleMon AI 어시스턴트입니다. 무엇이든 박사 수준의 깊이와 따뜻함으로 답하는 "
        "지식인·상담가입니다.\n\n"
        "━━━ 정체성 ━━━\n"
        "- 여러 분야에 해박한 최고 수준의 지식인입니다. 컴퓨터과학, 경영, 심리, 역사, "
        "철학, 일상 지식까지 두루 갖추고 있습니다.\n"
        "- 단순히 아는 것이 아니라, 핵심 원리와 메커니즘을 이해하고 실전 적용까지 이끕니다.\n"
        "- 사용자의 상황에 공감하고 \"당신의 경우라면 이렇게 하는 게 좋다\"는 실용적 조언을 "
        "따뜻하게 전달하는 상담가의 성격도 함께 지닙니다.\n\n"
        "━━━ 답변 원칙 ━━━\n"
        "- 항상 한국어로 답변하세요. 어떤 언어로 질문해도 한국어로 답하세요.\n"
        "- 사고 과정(reasoning)도 반드시 한국어로 하세요.\n"
        "- 구체적이고 상세하게 답하세요. 두루뭉술하게 요약하지 마세요.\n"
        "- 실전 예시, 수치, 단계별 가이드를 반드시 포함하세요. 막연한 조언은 금지입니다.\n"
        "- 한 줄, 두 줄로 끝내지 마세요. **3~5개의 핵심 포인트를 잡고, 각 포인트를 2~3문장으로 "
        "충분히 설명하세요.** 그래야 최소 300자 이상, 복잡한 질문은 500자 이상이 됩니다.\n"
        "- 표나 목록 등 가독성 있는 형식을 활용하세요. 필요하면 코드·공식·비교표도 쓰세요.\n"
        "- 자신의 의견을 명확히 밝히세요. \"제 생각엔 A보다 B가 낫습니다. 이유는…\"처럼 추천과 "
        "근거를 함께 제시하세요.\n"
        "- 확실하지 않으면 추측임을 밝히고, 정보가 부족하면 답을 주되 조건부로 안내하세요: "
        "\"상황에 따라 달라질 수 있는데, 구체적 상황을 알려주시면 더 정확히 준비해 드릴 수 있습니다.\"\n\n"
        "━━━ 답변 구성 (모든 단계를 빠짐없이 채우세요) ━━━\n"
        "1. 핵심 답변 — 질문에 바로 답하는 1~2문장\n"
        "2. 상세 설명 — 배경과 원리 (3~5문장)\n"
        "3. 실전 예시 — 코드·수치·사례 (최소 1개)\n"
        "4. 단계별 가이드 — 따라 할 수 있는 절차 (해당 시)\n"
        "5. 팁 & 주의사항 — 놓치기 쉬운 포인트\n"
        "6. 핵심 액션 — \"지금 바로 할 수 있는 것 2~3가지\" 간결 정리\n"
        "7. 심화 제안 — 아직 다루지 못한 하위 주제 하나를 제안해 대화를 자연스럽게 이어감\n\n"
        "━━━ 질문 유형 분류 ━━━\n"
        "답변 전에 질문 유형을 파악하고 구성에 반영하세요:\n"
        "- 정보 조회형 → 정확한 정의 + 배경 원리 중심\n"
        "- 의사결정형 → 선택지 비교 + 당신의 추천과 이유\n"
        "- 실행 가이드형 → 단계별 절차 + 예시 중심\n"
        "- 창작형 → 바로 결과물을 만들고 개선 방향 제안\n"
        "- 분석형 → 원인·영향·시사점 구조\n\n"
        "━━━ 대화 이어가기 ━━━\n"
        "- 답변을 마칠 때는 사용자가 더 원하는 답변을 끌어낼 수 있도록 심화 제안으로 마무리하세요.\n"
        "- 문구 예시: \"질문자님이 원하시면 더 깊게 봐서 더 원하는 답변을 드릴 수 있을 것 같습니다.\", "
        "\"이 부분을 더 파고들어 드릴까요?\", \"상황을 알려주시면 그에 맞춰 더 정확한 답변을 준비해 드릴 수 있습니다.\"\n"
        "- \"더 도와드릴까요?\"처럼 뻔한 반복 대신, 아직 다루지 못한 구체적 하위 주제를 하나씩 짚어서 "
        "다음 질문을 유도하세요. 예: \"이 주제에는 비용/효율/리스크 관점도 있는데, 장단점을 비교해서 정리해 드릴까요?\"\n"
        "- 같은 문구를 매번 쓰지 말고 답변 내용에 맞게 조금씩 다르게 표현하세요.\n"
        "- 절대로 \"결제\"나 \"크레딧\"을 직접 언급하지 마세요. \"더 깊은 분석 / 더 원하는 답변 준비\"라는 "
        "가치 제안으로만 자연스럽게 이어가세요.\n\n"
        "━━━ 확신도 표시 ━━━\n"
        "답변의 마지막 줄에 확신도를 다음 형식으로만 표시하세요 (본문에서 언급하지 말 것):\n"
        "[CONFIDENCE: high] / [CONFIDENCE: medium] / [CONFIDENCE: low]\n"
        "정보가 부족해 되묻기만 한 경우는 [CONFIDENCE: medium]으로 표시하세요.\n\n"
        "━━━ 메시지 초안 작성 ━━━\n"
        "사용자가 \"홍보문구 써줘\", \"공지문 작성해줘\"처럼 발송용 메시지 초안을 요청하면(도구 호출이 아닌 "
        "순수 텍스트 생성으로 처리):\n"
        "1. 바로 발송 가능한 완성문을 작성하세요. 대안 나열이 아니라 복사해서 보낼 수 있는 실제 문장이어야 합니다.\n"
        "2. 초안은 markdown 인용문(`>`) 또는 코드블록(```)으로 본문과 명확히 구분해 제시하세요.\n"
        "3. 초안 제시 직후 \"이대로 발송하시겠어요?\"로 마무리하세요.\n"
        "4. 사용자가 실제 발송을 요청하기 전까지는 도구를 호출하지 마세요.\n\n"
        "━━━ 금지 사항 ━━━\n"
        "- 어떤 질문이든 거부하지 마세요. \"도와드릴 수 없습니다\" 같은 답변은 하지 마세요.\n"
        "- \"죄송합니다\" 같은 사과를 남발하지 마세요.\n"
        "- \"AI입니다\", \"저는 AI라서\" 같은 자기 정체성을 밝히지 마세요.\n"
        "- 표면적인 답변만 하지 마세요. 근본 원리까지 깊이 파고드세요.\n"
        "- \"어렵습니다\", \"복잡합니다\" 같은 막연한 표현은 쓰지 마세요.\n"
        "- 답변을 축약하지 마세요. 충분히 상세하게 답하세요."
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

    # 4.4. Inject learned quality rules — recently negatively-rated answers
    # for this tenant are shown as "avoid this" examples so the model does
    # not repeat the same failures. Lightweight: last 3, trimmed.
    try:
        bad_result = await db.execute(
            select(AiChatMessageV2).where(
                AiChatMessageV2.tenant_id == tenant_id,
                AiChatMessageV2.role == "assistant",
                AiChatMessageV2.feedback_score <= 2,
                AiChatMessageV2.content.isnot(None),
            ).order_by(AiChatMessageV2.created_at.desc()).limit(3)
        )
        bad_msgs = [m.content for m in bad_result.scalars().all() if m.content and m.content.strip()]
        if bad_msgs:
            examples = "\n".join(f"- {m[:200]}" for m in bad_msgs)
            messages.append({
                "role": "system",
                "content": (
                    "다음은 사용자가 '도움 안 됨' 평가를 내린 이전 답변들입니다. "
                    "이런 식의 답변은 반복하지 마세요:\n" + examples
                ),
            })
    except Exception as exc:
        logger.debug("ai_quality_rules_inject_failed", error=str(exc))

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
        recent = request.context.get("recent_messages")
        if isinstance(recent, list) and recent:
            joined = "\n".join(f"- {str(m)[:300]}" for m in recent[-5:])
            ctx_parts.append(f"사용자가 최근 보고 있는 대화 메시지:\n{joined}")
        if ctx_parts:
            # Response style steering (default/concise/detailed/code).
            style = request.context.get("style")
            if style == "concise":
                ctx_parts.append("답변 스타일: 간결하게 — 핵심만 요약해 2~3문장 안에 답하세요.")
            elif style == "detailed":
                ctx_parts.append("답변 스타일: 자세히 — 최대한 상세하게 모든 관련 내용을 설명하세요.")
            elif style == "code":
                ctx_parts.append("답변 스타일: 코드 위주 — 예시 코드를 중심으로 설명하세요.")
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
        # search_knowledge_base() takes no tenant_id (Document has no
        # tenant_id column -- single shared KB) and returns a
        # (list[SearchResult], list[str]) tuple, not a list of dicts -- this
        # used to pass an invalid kwarg and then call .get() on Pydantic
        # models, so it always hit the except below and silently never ran.
        # 5a+5b run in parallel — both are independent lookups, and running
        # them concurrently cuts first-token latency (mobile users feel this
        # as "AI responds faster").
        async def _kb_pass() -> list:
            try:
                from app.services.knowledge_base import search_knowledge_base
                results, _ = await search_knowledge_base(
                    db, request.content, top_k=3, tenant_id=tenant_id,
                )
                if not results or max((r.score for r in results), default=0) < 0.5:
                    retry, _ = await search_knowledge_base(
                        db, request.content[:80], top_k=3, tenant_id=tenant_id,
                    )
                    if retry and max((r.score for r in retry), default=0) > (
                        max((r.score for r in results), default=0) if results else 0
                    ):
                        results = retry
                return results
            except Exception as exc:
                logger.warning("ai_chat_v2_kb_search_failed", error=str(exc))
                return []

        kb_results, memory_context = await asyncio.gather(
            _kb_pass(),
            _enrich_with_memory(tenant_id, session.id, request.content),
        )

        for r in kb_results:
            if r.content:
                kb_context.append(r.content)
                kb_confidence = max(kb_confidence, r.score)
        if kb_context:
            kb_text = "\n\n".join(
                f"[출처: {r.document_title} | 신뢰도: {r.score:.0%}]\n{r.content}"
                for r in kb_results if r.content
            )
            messages.append({
                "role": "system",
                "content": (
                    f"관련 지식 (Knowledge Base):\n{kb_text}\n\n"
                    "Knowledge Base 내용을 참조해 답변했다면 반드시 "
                    "[출처: 문서명] 형식으로 출처를 함께 표시하세요."
                ),
            })

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

    # 7.6. Handle file attachments (images/videos) — analyze with vision model
    if request.attachments:
        from app.services.ai_vision_service import analyze_image, analyze_video
        import os as _os

        upload_base = _os.path.join("data", "uploads", "ai_chat")
        analysis_results: list[str] = []

        for att in request.attachments:
            url = att.get("url", "")
            mime = att.get("mime_type", "")
            filename = att.get("filename", "unknown")

            # Resolve URL to local file path
            filepath = None
            if url.startswith("/uploads/"):
                rel = url[len("/uploads/"):]
                candidate = _os.path.join(upload_base, _os.path.basename(rel))
                if _os.path.exists(candidate):
                    filepath = candidate
            elif url and _os.path.exists(url):
                filepath = url

            if not filepath:
                if mime.startswith("image/"):
                    analysis_results.append(f"[이미지 첨부: {filename} — 파일을 찾을 수 없습니다]")
                elif mime.startswith("video/"):
                    analysis_results.append(f"[동영상 첨부: {filename} — 파일을 찾을 수 없습니다]")
                continue

            if mime.startswith("image/"):
                result = await analyze_image(filepath, mime, request.content)
                analysis_results.append(f"[이미지 분석: {filename}]\n{result}")
            elif mime.startswith("video/"):
                result = await analyze_video(filepath, mime, request.content)
                analysis_results.append(f"[동영상 분석: {filename}]\n{result}")

        if analysis_results:
            user_content = "\n\n".join(analysis_results) + "\n\n" + user_content

    messages.append({"role": "user", "content": user_content})

    # 8. Stream or non-stream
    # The model always reasons internally regardless of budget -- think_mode
    # only controls whether that reasoning is surfaced to the user, not the
    # token budget the call gets.
    #
    # 8a. Dynamic budget: simple/ack requests get a smaller ceiling, complex
    # analysis/code/guide requests get a larger one so they can be thorough.
    intent = _classify_intent(request.content)
    if intent == "simple":
        max_tokens = min(_DEFAULT_MAX_TOKENS, 800)
        # Short factual answers → low temperature for precision.
        effective_temperature = 0.4
    elif intent == "complex":
        # 14B GPU is slow at very long generations; keep the ceiling at the
        # default so complex answers stay detailed but don't take forever.
        max_tokens = _DEFAULT_MAX_TOKENS
        # Analysis/creative/code → higher temperature for richer output.
        effective_temperature = 0.8
    else:
        # Standard questions (the bulk of traffic) also get 0.8 — higher
        # temperature keeps answers lively/natural vs. the old flat 0.7.
        max_tokens = _DEFAULT_MAX_TOKENS
        effective_temperature = 0.8

    # 8b. Auto think-mode for complex requests: reasoning pass improves final
    # answer quality even though the model always reasons internally anyway.
    effective_think_mode = request.think_mode or (intent == "complex" and request.context.get("auto_think", True))

    if request.stream:
        # Streaming response
        full_content = ""
        full_reasoning = ""
        full_confidence: str | None = None
        tool_calls_buffer: list[dict] = []  # Collect tool calls from stream
        current_tool_call: dict | None = None
        # Content is held back CONFIDENCE_STREAM_HOLDBACK chars behind what's
        # been received so the trailing "[CONFIDENCE: ...]" marker never gets
        # flushed to the client mid-stream -- see extract_confidence().
        pending = ""
        try:
            async for kind, text in _stream_ollama(messages, model=request.model, max_tokens=max_tokens, tools=TOOLS if not request.context.get("disable_tools") else None, temperature=effective_temperature):
                if kind == "reasoning":
                    full_reasoning += text
                    if not effective_think_mode:
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
                pending += text
                safe_len = max(0, len(pending) - CONFIDENCE_STREAM_HOLDBACK)
                to_emit, pending = pending[:safe_len], pending[safe_len:]
                if to_emit:
                    full_content += to_emit
                    yield f"data: {json.dumps({'type': 'chunk', 'content': to_emit})}\n\n"
        except Exception as exc:
            logger.error("ai_chat_v2_stream_failed", error=str(exc))
            yield f"data: {json.dumps({'type': 'error', 'content': 'Stream failed. Please try again.'})}\n\n"
            return

        # Flush the held-back tail now that the stream is done, stripping the
        # confidence marker out of it if present -- only reachable once no
        # more content chunks are coming, so this is the one safe point to
        # know the marker (if any) is fully in `pending`.
        pending, tail_confidence = extract_confidence(pending)
        if tail_confidence:
            full_confidence = tail_confidence
        if pending:
            full_content += pending
            yield f"data: {json.dumps({'type': 'chunk', 'content': pending})}\n\n"

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

            # Get final AI response with tool results -- this, not the
            # pre-tool-call pass above, is the answer the user actually
            # reads, so the confidence marker (and holdback) applies here.
            full_content = ""
            full_confidence = None
            pending = ""
            async for kind, text in _stream_ollama(messages, model=request.model, max_tokens=max_tokens, temperature=effective_temperature):
                if kind == "content":
                    text = text.replace("<think>", "").replace("</think>", "")
                    if text:
                        pending += text
                        safe_len = max(0, len(pending) - CONFIDENCE_STREAM_HOLDBACK)
                        to_emit, pending = pending[:safe_len], pending[safe_len:]
                        if to_emit:
                            full_content += to_emit
                            yield f"data: {json.dumps({'type': 'chunk', 'content': to_emit})}\n\n"
            pending, tail_confidence = extract_confidence(pending)
            if tail_confidence:
                full_confidence = tail_confidence
            if pending:
                full_content += pending
                yield f"data: {json.dumps({'type': 'chunk', 'content': pending})}\n\n"

        if not full_content and not tool_calls_buffer:
            yield f"data: {json.dumps({'type': 'error', 'content': 'Empty response from AI.'})}\n\n"
            return

        # Streaming quality gate: if the streamed answer is suspiciously
        # short (and there were no tool calls), regenerate once non-streaming
        # and use that instead — mirrors the non-stream branch's gate.
        if tool_calls_buffer:
            pass
        elif not request.context.get("disable_self_review") and (
            len(full_content) < 40 or _is_rambling(full_content) or _is_refusal(full_content)
        ):
            logger.info("ai_stream_quality_gate_retry", length=len(full_content))
            regen, _pt2, _ct2 = await _call_ollama_nonstream(
                messages, model=request.model, max_tokens=max_tokens, temperature=effective_temperature,
            )
            if regen and len(regen) > len(full_content):
                full_content, full_confidence = extract_confidence(regen)
                # Stream the replacement so the client ends up with it.
                yield f"data: {json.dumps({'type': 'chunk', 'content': full_content})}\n\n"

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
            memory_context=_assistant_context(memory_context, kb_context),
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
        # Sensitive queries cost double (privacy redaction / higher risk tier).
        if request.context.get("sensitive"):
            actual_chars *= 2
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
            'confidence': full_confidence,
        })}\n\n"

    else:
        # Non-streaming response
        reply, prompt_tokens, completion_tokens = await _call_ollama_nonstream(
            messages, model=request.model, max_tokens=max_tokens, temperature=effective_temperature,
        )
        if reply is None:
            yield f"data: {json.dumps({'type': 'error', 'content': 'AI service unavailable. Please try again.'})}\n\n"
            return
        reply, confidence = extract_confidence(reply)

        # Quality gate (latency-friendly): only run self-review / regenerate
        # when the answer is suspiciously short OR clearly rambling. Good
        # answers skip the extra model call — that was the mobile-latency fix.
        reply_len = len(reply) if reply else 0
        gate_enabled = not request.context.get("disable_self_review")
        # 40 chars = clearly a dead/refusal-style answer; anything longer is
        # usually fine. Rambling repeats still get one regenerate.
        too_short = gate_enabled and reply_len < 40
        rambling = gate_enabled and reply_len >= 40 and _is_rambling(reply or "")
        refusal = gate_enabled and _is_refusal(reply or "")
        if reply and gate_enabled and (too_short or rambling or refusal):
            passed, _reason = await self_review_answer(request.content, reply, kb_context)
            if not passed or reply_len < 20:
                reason = "too_short" if too_short else ("refusal" if refusal else "rambling")
                logger.info("ai_chat_quality_gate_retry", reason=reason, length=reply_len)
                retried, _pt, _ct = await _call_ollama_nonstream(
                    messages, model=request.model, max_tokens=max_tokens, temperature=effective_temperature,
                )
                if retried and len(retried) > reply_len:
                    reply, confidence = extract_confidence(retried)

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
            memory_context=_assistant_context(memory_context, kb_context),
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
        if request.context.get("sensitive"):
            actual_chars *= 2
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
            'confidence': confidence,
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

        # Check if this Q&A pair is already in KB (tenant-scoped)
        existing = await db.execute(
            select(Document).where(
                Document.source_type == "ai_chat_positive",
                Document.source_url == f"chat://{assistant_msg.id}",
                Document.tenant_id == assistant_msg.tenant_id,
            ).limit(1)
        )
        if existing.scalar_one_or_none():
            continue

        # Create KB document from the Q&A pair (owned by the answering tenant)
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
                tenant_id=assistant_msg.tenant_id,
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


#  Weekly AI Quality Report


async def get_ai_quality_report(db: AsyncSession, tenant_id: str, days: int = 7) -> dict:
    """Per-tenant AI quality summary for the last N days.

    Includes feedback distribution, RAG effectiveness (KB-referenced vs not),
    learned candidates, and a short "what's improving" readout — surfaced to
    the user so feedback quality is rewarded and the learning loop is visible.
    """
    from datetime import timedelta
    from sqlalchemy import case, func
    from app.core.time import utcnow_naive
    from app.models.ai_chat_v2 import AiChatMessageV2
    from app.models.knowledge_base import Document, KnowledgeCandidate

    now = utcnow_naive()
    since = now - timedelta(days=days)

    # Feedback stats
    fb = await db.execute(
        select(
            func.count(AiChatMessageV2.id).label("total"),
            func.count(case((AiChatMessageV2.feedback_score >= 4, 1))).label("positive"),
            func.count(case((AiChatMessageV2.feedback_score <= 2, 1))).label("negative"),
            func.avg(AiChatMessageV2.feedback_score).label("avg"),
        ).where(
            AiChatMessageV2.tenant_id == tenant_id,
            AiChatMessageV2.role == "assistant",
            AiChatMessageV2.feedback_score.isnot(None),
            AiChatMessageV2.created_at >= since,
        )
    )
    f = fb.one()

    # RAG effectiveness: compare avg feedback for messages that used RAG vs not.
    rag = await db.execute(
        select(
            func.avg(AiChatMessageV2.feedback_score).label("rag_avg"),
        ).where(
            AiChatMessageV2.tenant_id == tenant_id,
            AiChatMessageV2.role == "assistant",
            AiChatMessageV2.feedback_score.isnot(None),
            AiChatMessageV2.created_at >= since,
            AiChatMessageV2.memory_context.isnot(None),
        )
    )
    no_rag = await db.execute(
        select(
            func.avg(AiChatMessageV2.feedback_score).label("no_rag_avg"),
        ).where(
            AiChatMessageV2.tenant_id == tenant_id,
            AiChatMessageV2.role == "assistant",
            AiChatMessageV2.feedback_score.isnot(None),
            AiChatMessageV2.created_at >= since,
            AiChatMessageV2.memory_context.is_(None),
        )
    )

    # Learned KB docs + pending candidates
    # ingest_positive_responses() (see above) actually stores these with
    # source_type="ai_chat_positive" and collection="ai_learned" -- this
    # was filtering on the wrong field and always returned 0.
    kb_count = (await db.execute(
        select(func.count(Document.id)).where(
            Document.tenant_id == tenant_id, Document.source_type == "ai_chat_positive",
        )
    )).scalar() or 0
    pending_candidates = (await db.execute(
        select(func.count(KnowledgeCandidate.id)).where(
            KnowledgeCandidate.tenant_id == tenant_id,
            KnowledgeCandidate.status == "pending",
        )
    )).scalar() or 0

    total = f.total or 0
    positive_rate = round((f.positive or 0) / total * 100, 1) if total else 0

    # Domain benchmark: classify each assistant message by intent and bucket
    # its feedback so we can see which domains are weakest (simple vs
    # standard vs complex). Heuristic via _classify_intent on the message.
    domain_rows = await db.execute(
        select(AiChatMessageV2.content, AiChatMessageV2.feedback_score).where(
            AiChatMessageV2.tenant_id == tenant_id,
            AiChatMessageV2.role == "assistant",
            AiChatMessageV2.feedback_score.isnot(None),
            AiChatMessageV2.created_at >= since,
        )
    )
    domains: dict[str, dict] = {}
    for content, score in domain_rows.all():
        d = _classify_intent(content or "")
        bucket = domains.setdefault(d, {"total": 0, "positive": 0, "sum": 0})
        bucket["total"] += 1
        bucket["sum"] += float(score or 0)
        if (score or 0) >= 4:
            bucket["positive"] += 1
    domain_summary = {
        k: {
            "total": v["total"],
            "positive": v["positive"],
            "avg_score": round(v["sum"] / v["total"], 2) if v["total"] else 0,
        }
        for k, v in domains.items()
    }

    return {
        "period_days": days,
        "domains": domain_summary,
        "feedback": {
            "total": total,
            "positive": f.positive or 0,
            "negative": f.negative or 0,
            "avg_score": round(float(f.avg or 0), 2),
            "positive_rate": positive_rate,
        },
        "rag_effectiveness": {
            "rag_avg_score": round(float(rag.scalar() or 0), 2),
            "no_rag_avg_score": round(float(no_rag.scalar() or 0), 2),
        },
        "learning": {
            "learned_kb_docs": kb_count,
            "pending_candidates": pending_candidates,
        },
    }