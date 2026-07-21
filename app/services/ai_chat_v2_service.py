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
from app.services.ai_core_service import call_deepseek, search_memory, store_memory

logger = get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────────

_MAX_HISTORY_MESSAGES = 50
_MAX_INPUT_CHARS = 10000
_DEFAULT_MAX_TOKENS = 2000
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


# ── Session Management ──────────────────────────────────────────────────


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


# ── Message History ─────────────────────────────────────────────────────


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


# ── Prompt Templates ────────────────────────────────────────────────────


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


def _apply_template(content: str, variables: dict[str, str]) -> str:
    """Replace {{variable}} placeholders with actual values."""
    if not variables:
        return content

    def _replace(match: re.Match) -> str:
        key = match.group(1).strip()
        return variables.get(key, match.group(0))

    return re.sub(r"\{\{(\w+)\}\}", _replace, content)


# ── Memory Integration ──────────────────────────────────────────────────


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


# ── Streaming DeepSeek Call ─────────────────────────────────────────────


async def _stream_deepseek(
    messages: list[dict],
    model: str = "deepseek-chat",
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> AsyncGenerator[str, None]:
    """Stream response from DeepSeek API, yielding content chunks."""
    client = _get_client()
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": True,
    }

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
                        content = delta.get("content", "")
                        if content:
                            yield content
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
    model: str = "deepseek-chat",
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> tuple[str | None, int, int]:
    """Non-streaming call with token tracking. Returns (content, prompt_tokens, completion_tokens)."""
    client = _get_client()
    payload = {
        "model": model,
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
            content = data["choices"][0]["message"]["content"]
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


# ── Main Chat Entry Point ───────────────────────────────────────────────


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
        "You are TeleMon AI Assistant. You help users manage their Telegram "
        "marketing operations. Be helpful, concise, and professional."
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

    # 5. Add memory context if enabled
    memory_context: list[str] = []
    if request.use_memory:
        memory_context = await _enrich_with_memory(tenant_id, session.id, request.content)
        if memory_context:
            memory_text = "\n".join(f"- {m}" for m in memory_context)
            messages.append({
                "role": "system",
                "content": f"Relevant context from memory:\n{memory_text}",
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

    messages.append({"role": "user", "content": user_content})

    # 8. Stream or non-stream
    if request.stream:
        # Streaming response
        full_content = ""
        try:
            async for chunk in _stream_deepseek(messages, model=request.model):
                full_content += chunk
                yield f"data: {json.dumps({'type': 'chunk', 'content': chunk})}\n\n"
        except Exception as exc:
            logger.error("ai_chat_v2_stream_failed", error=str(exc))
            yield f"data: {json.dumps({'type': 'error', 'content': 'Stream failed. Please try again.'})}\n\n"
            return

        if not full_content:
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

        await db.commit()

        yield f"data: {json.dumps({
            'type': 'done',
            'message_id': assistant_msg.id,
            'tokens_prompt': prompt_tokens,
            'tokens_completion': completion_tokens,
            'latency_ms': latency_ms,
        })}\n\n"

    else:
        # Non-streaming response
        reply, prompt_tokens, completion_tokens = await _call_deepseek_nonstream(
            messages, model=request.model,
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

        await db.commit()

        yield f"data: {json.dumps({
            'type': 'done',
            'message_id': assistant_msg.id,
            'content': reply,
            'tokens_prompt': prompt_tokens,
            'tokens_completion': completion_tokens,
            'latency_ms': latency_ms,
        })}\n\n"


# ── Conversation Search ─────────────────────────────────────────────────


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


# ── Usage Stats ─────────────────────────────────────────────────────────


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


# ── Message Feedback ────────────────────────────────────────────────────


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
    return msg