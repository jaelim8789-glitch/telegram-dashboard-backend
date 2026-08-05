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

from app.api.deps import get_current_identity, Identity, require_account_tenant_access, require_admin
from app.core.logging import get_logger
from app.core.time import utcnow_naive
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
    delete_template,
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


#  Session Endpoints 


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


#  Chat Endpoint (SSE Streaming) 


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


#  Message History 


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


#  Message Feedback 


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


#  Conversation Search 


@router.post("/search", response_model=SearchResponse)
async def search_endpoint(
    payload: SearchRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Search messages across sessions."""
    return await search_conversations(db, identity.tenant_id, payload)


#  Prompt Template Endpoints 


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


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template_endpoint(
    template_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Delete a prompt template."""
    deleted = await delete_template(db, template_id, identity.tenant_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")


#  Usage Stats


@router.get("/stats", response_model=UsageStats)
async def usage_stats_endpoint(
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get AI Chat 2.0 usage statistics."""
    return await get_usage_stats(db, identity.tenant_id)


#  AI Learning — Feedback Analytics


@router.get("/analytics/feedback")
async def feedback_analytics_endpoint(
    days: int = Query(default=7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(require_admin),
):
    """Get feedback analytics for AI Learning dashboard.

    Returns feedback distribution, top/worst responses, and daily trends.
    Admin-only endpoint.
    """
    from datetime import timedelta
    from sqlalchemy import func, case, and_

    now = utcnow_naive()
    since = now - timedelta(days=days)

    # Overall stats
    base_query = select(
        func.count(AiChatMessageV2.id).label("total"),
        func.count(case((AiChatMessageV2.feedback_score >= 4, 1))).label("positive"),
        func.count(case((AiChatMessageV2.feedback_score <= 2, 1))).label("negative"),
        func.avg(AiChatMessageV2.feedback_score).label("avg_score"),
    ).where(
        AiChatMessageV2.role == "assistant",
        AiChatMessageV2.feedback_score.isnot(None),
        AiChatMessageV2.created_at >= since,
    )

    result = await db.execute(base_query)
    row = result.one()

    # Daily trends
    daily_query = select(
        func.date(AiChatMessageV2.created_at).label("date"),
        func.count(AiChatMessageV2.id).label("total"),
        func.count(case((AiChatMessageV2.feedback_score >= 4, 1))).label("positive"),
        func.count(case((AiChatMessageV2.feedback_score <= 2, 1))).label("negative"),
    ).where(
        AiChatMessageV2.role == "assistant",
        AiChatMessageV2.feedback_score.isnot(None),
        AiChatMessageV2.created_at >= since,
    ).group_by(func.date(AiChatMessageV2.created_at)).order_by(func.date(AiChatMessageV2.created_at))

    daily_result = await db.execute(daily_query)
    daily_rows = daily_result.all()

    # Top positive responses (score >= 4, with content)
    top_query = select(
        AiChatMessageV2.id,
        AiChatMessageV2.content,
        AiChatMessageV2.feedback_score,
        AiChatMessageV2.created_at,
    ).where(
        AiChatMessageV2.role == "assistant",
        AiChatMessageV2.feedback_score >= 4,
        AiChatMessageV2.created_at >= since,
    ).order_by(AiChatMessageV2.feedback_score.desc()).limit(20)

    top_result = await db.execute(top_query)
    top_rows = top_result.all()

    # Worst responses (score <= 2, with content)
    worst_query = select(
        AiChatMessageV2.id,
        AiChatMessageV2.content,
        AiChatMessageV2.feedback_score,
        AiChatMessageV2.created_at,
    ).where(
        AiChatMessageV2.role == "assistant",
        AiChatMessageV2.feedback_score <= 2,
        AiChatMessageV2.created_at >= since,
    ).order_by(AiChatMessageV2.feedback_score.asc()).limit(20)

    worst_result = await db.execute(worst_query)
    worst_rows = worst_result.all()

    return {
        "period_days": days,
        "summary": {
            "total": row.total or 0,
            "positive": row.positive or 0,
            "negative": row.negative or 0,
            "avg_score": round(float(row.avg_score or 0), 2),
        },
        "daily": [
            {"date": str(r.date), "total": r.total, "positive": r.positive, "negative": r.negative}
            for r in daily_rows
        ],
        "top_responses": [
            {"id": r.id, "content": r.content[:200], "score": r.feedback_score, "created_at": str(r.created_at)}
            for r in top_rows
        ],
        "worst_responses": [
            {"id": r.id, "content": r.content[:200], "score": r.feedback_score, "created_at": str(r.created_at)}
            for r in worst_rows
        ],
    }


@router.post("/analytics/ingest-to-kb")
async def ingest_positive_to_kb_endpoint(
    min_score: int = Query(default=4, ge=1, le=5),
    days: int = Query(default=7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(require_admin),
):
    """Ingest positively-rated Q&A pairs into the Knowledge Base.

    Finds assistant messages with feedback_score >= min_score from the last N days,
    pairs them with the preceding user message, and ingests them into KB as
    'ai_chat_positive' source type.
    Admin-only endpoint.
    """
    from datetime import timedelta
    from app.services.ai_chat_v2_service import ingest_positive_responses

    count = await ingest_positive_responses(db, identity.tenant_id, min_score=min_score, days=days)
    return {"ingested": count, "min_score": min_score, "days": days}


#  Tool Confirmation


@router.post("/confirm-tool")
async def confirm_tool_endpoint(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Execute a write tool after user confirmation.

    Expected payload:
    {
        "tool_name": "send_broadcast",
        "arguments": {"account_id": "...", "message": "...", ...},
        "session_id": "...",
    }
    """
    from app.api.ai_tools import execute_tool, TOOL_META

    tool_name = payload.get("tool_name")
    arguments = payload.get("arguments", {})

    if not tool_name:
        raise HTTPException(status_code=400, detail="tool_name is required")

    meta = TOOL_META.get(tool_name, {})
    if not meta.get("requires_confirmation"):
        raise HTTPException(status_code=400, detail="This tool does not require confirmation")

    result = await execute_tool(tool_name, arguments, identity)

    if not result.success:
        raise HTTPException(status_code=400, detail=result.error)

    # Save the tool result as an assistant message
    session_id = payload.get("session_id")
    if session_id:
        from app.models.ai_chat_v2 import AiChatMessageV2
        msg = AiChatMessageV2(
            id=str(uuid.uuid4()),
            session_id=session_id,
            tenant_id=identity.tenant_id,
            role="assistant",
            content=f"✅ {meta.get('label', tool_name)} 실행 완료\n\n{json.dumps(result.result, ensure_ascii=False, default=str)}",
            model="tool",
        )
        db.add(msg)
        await db.commit()

    return {"success": True, "result": result.result}