"""AI Chat 2.0 API Router.

Endpoints:
- Session CRUD (create, list, get, update, delete/archive)
- Chat (SSE streaming + non-streaming)
- Message history
- Conversation search
- Prompt template CRUD
- Usage stats
- Message feedback
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_account_tenant_access
from app.core.logging import get_logger
from app.database import get_db
from app.schemas.ai_chat_v2 import (
    ChatRequest,
    MessageFeedback,
    MessageRead,
    PromptTemplateCreate,
    PromptTemplateRead,
    PromptTemplateUpdate,
    SearchRequest,
    SearchResponse,
    SessionCreate,
    SessionRead,
    SessionSummary,
    SessionUpdate,
    UsageStats,
)
from app.services.ai_chat_v2_service import (
    chat,
    create_session,
    create_template,
    delete_session,
    get_default_template,
    get_session,
    get_session_messages,
    get_usage_stats,
    list_sessions,
    search_conversations,
    submit_message_feedback,
    update_session,
)

router = APIRouter(prefix="/api/ai-chat-v2", tags=["ai-chat-v2"])
logger = get_logger(__name__)


# ── Session Endpoints ───────────────────────────────────────────────────


@router.post("/sessions", response_model=SessionRead, status_code=status.HTTP_201_CREATED)
async def create_session_endpoint(
    payload: SessionCreate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Create a new chat session."""
    session = await create_session(db, identity.tenant_id, payload)
    logger.info("session_created", session_id=session.id)
    return session


@router.get("/sessions", response_model=list[SessionSummary])
async def list_sessions_endpoint(
    include_archived: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """List sessions for the current tenant."""
    return await list_sessions(db, identity.tenant_id, include_archived, limit, offset)


@router.get("/sessions/{session_id}", response_model=SessionRead)
async def get_session_endpoint(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get a session by ID."""
    session = await get_session(db, session_id, identity.tenant_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")
    return session


@router.put("/sessions/{session_id}", response_model=SessionRead)
async def update_session_endpoint(
    session_id: str,
    payload: SessionUpdate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Update a session."""
    session = await update_session(db, session_id, identity.tenant_id, payload)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")
    return session


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session_endpoint(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Archive (soft-delete) a session."""
    deleted = await delete_session(db, session_id, identity.tenant_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")


# ── Chat Endpoint (SSE Streaming) ───────────────────────────────────────


@router.post("/chat")
async def chat_endpoint(
    payload: ChatRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Send a message and stream the AI response via SSE.

    Returns a Server-Sent Events stream with:
    - data: {"type": "chunk", "content": "..."}  (streaming tokens)
    - data: {"type": "done", "message_id": "...", ...}  (final)
    - data: {"type": "error", "content": "..."}  (on failure)
    """
    return StreamingResponse(
        chat(db, identity.tenant_id, payload),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Message History ─────────────────────────────────────────────────────


@router.get("/sessions/{session_id}/messages", response_model=list[MessageRead])
async def get_messages(
    session_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get messages for a session (oldest first)."""
    # Verify session belongs to tenant
    session = await get_session(db, session_id, identity.tenant_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")
    return await get_session_messages(db, session_id, identity.tenant_id, limit, offset)


# ── Message Feedback ────────────────────────────────────────────────────


@router.post("/messages/{message_id}/feedback", response_model=MessageRead)
async def feedback_endpoint(
    message_id: str,
    payload: MessageFeedback,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Submit feedback for a message."""
    msg = await submit_message_feedback(db, message_id, identity.tenant_id, payload.score, payload.comment)
    if msg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found.")
    return msg


# ── Conversation Search ─────────────────────────────────────────────────


@router.post("/search", response_model=SearchResponse)
async def search_endpoint(
    payload: SearchRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Search messages across sessions."""
    return await search_conversations(db, identity.tenant_id, payload)


# ── Prompt Template Endpoints ───────────────────────────────────────────


@router.post("/templates", response_model=PromptTemplateRead, status_code=status.HTTP_201_CREATED)
async def create_template_endpoint(
    payload: PromptTemplateCreate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Create a prompt template."""
    template = await create_template(db, identity.tenant_id, payload)
    logger.info("template_created", template_id=template.id)
    return template


@router.get("/templates", response_model=list[PromptTemplateRead])
async def list_templates(
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """List all prompt templates for the tenant."""
    from sqlalchemy import select
    from app.models.ai_chat_v2 import AiChatPromptTemplate

    result = await db.execute(
        select(AiChatPromptTemplate)
        .where(AiChatPromptTemplate.tenant_id == identity.tenant_id)
        .order_by(AiChatPromptTemplate.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/templates/default", response_model=PromptTemplateRead | None)
async def get_default_template_endpoint(
    role: str = Query("system", pattern=r"^(system|user)$"),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get the default prompt template for a role."""
    return await get_default_template(db, identity.tenant_id, role)


# ── Usage Stats ─────────────────────────────────────────────────────────


@router.get("/stats", response_model=UsageStats)
async def usage_stats_endpoint(
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get AI Chat 2.0 usage statistics."""
    return await get_usage_stats(db, identity.tenant_id)