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

from fastapi import APIRouter, Depends, HTTPException, Query, status, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, get_current_tenant_id, Identity, require_account_tenant_access, require_admin
from app.core.logging import get_logger
from app.core.time import utcnow_naive
from app.database import get_db
from app.models.ai_chat_v2 import AiChatMessageV2
from app.schemas.ai_chat_v2 import (
    ChatRequest,
    MessageCreate,
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
    copy_message,
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
    tenant_id: str = Depends(get_current_tenant_id),
):
    """Create a new chat session.

    Uses get_current_tenant_id so admins (identity.tenant_id is None) still
    get their resolved admin tenant instead of a 500 FK error.
    """
    session = await create_session(db, tenant_id, payload)
    logger.info("session_created", session_id=session.id)
    return session


@router.get("/sessions", response_model=list[SessionSummary])
async def list_sessions_endpoint(
    include_archived: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    tenant_id: str = Depends(get_current_tenant_id),
):
    """List sessions for the current tenant (admin → resolved admin tenant)."""
    return await list_sessions(db, tenant_id, include_archived, limit, offset)


@router.get("/sessions/{session_id}", response_model=SessionRead)
async def get_session_endpoint(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    tenant_id: str = Depends(get_current_tenant_id),
):
    """Get a session by ID."""
    session = await get_session(db, session_id, tenant_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")
    return session


@router.put("/sessions/{session_id}", response_model=SessionRead)
async def update_session_endpoint(
    session_id: str,
    payload: SessionUpdate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    tenant_id: str = Depends(get_current_tenant_id),
):
    """Update a session."""
    session = await update_session(db, session_id, tenant_id, payload)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")
    return session


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session_endpoint(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    tenant_id: str = Depends(get_current_tenant_id),
):
    """Archive (soft-delete) a session."""
    deleted = await delete_session(db, session_id, tenant_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")


#  Chat Endpoint (SSE Streaming) 


@router.post("/chat")
async def chat_endpoint(
    payload: ChatRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    tenant_id: str = Depends(get_current_tenant_id),
):
    """Send a message and stream the AI response via SSE.

    Returns a Server-Sent Events stream with:
    - data: {"type": "chunk", "content": "..."}  (streaming tokens)
    - data: {"type": "done", "message_id": "...", ...}  (final)
    - data: {"type": "error", "content": "..."}  (on failure)
    """
    return StreamingResponse(
        chat(db, tenant_id, payload),
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
    tenant_id: str = Depends(get_current_tenant_id),
):
    """Get messages for a session (oldest first)."""
    # Verify session belongs to tenant
    session = await get_session(db, session_id, tenant_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")
    return await get_session_messages(db, session_id, tenant_id, limit, offset)


@router.post("/sessions/{session_id}/messages", response_model=MessageRead, status_code=status.HTTP_201_CREATED)
async def copy_message_endpoint(
    session_id: str,
    payload: MessageCreate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Copy a message verbatim into a session -- used by session branching
    on the frontend to seed a new conversation with an existing prefix. Not
    an AI call: no generation, no credit deduction."""
    msg = await copy_message(db, session_id, identity.tenant_id, payload.role, payload.content, payload.model)
    if msg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")
    return msg


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
    _admin: None = Depends(require_admin),
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
    _admin: None = Depends(require_admin),
):
    """Ingest positively-rated Q&A pairs into the Knowledge Base.

    Finds assistant messages with feedback_score >= min_score from the last N days,
    pairs them with the preceding user message, and ingests them into KB as
    'ai_chat_positive' source type.
    Admin-only endpoint.
    """
    from datetime import timedelta
    from app.services.ai_chat_v2_service import ingest_positive_responses

    count = await ingest_positive_responses(db, min_score=min_score, days=days)
    return {"ingested": count, "min_score": min_score, "days": days}


#  Knowledge Candidates


@router.get("/candidates")
async def list_candidates_endpoint(
    status: str = Query(default="pending", pattern="^(pending|approved|rejected|all)$"),
    db: AsyncSession = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    """List knowledge candidates for admin review."""
    from app.models.knowledge_base import KnowledgeCandidate

    # require_admin is a gate-only dependency (returns None, not an
    # Identity) -- this used to bind it as `identity: Identity` and then
    # filter by identity.tenant_id, which is an AttributeError on None. This
    # is an admin review queue across every tenant (+ 'guest'), not scoped
    # to one, so there's no tenant_id filter to apply anyway.
    stmt = select(KnowledgeCandidate)
    if status != "all":
        stmt = stmt.where(KnowledgeCandidate.status == status)
    stmt = stmt.order_by(KnowledgeCandidate.created_at.desc()).limit(100)

    result = await db.execute(stmt)
    candidates = result.scalars().all()

    return [
        {
            "id": c.id,
            "question": c.question[:200],
            "answer": c.answer[:500],
            "feedback_score": c.feedback_score,
            "feedback_count": c.feedback_count,
            "model_name": c.model_name,
            "tokens_used": c.tokens_used,
            "response_time_ms": c.response_time_ms,
            "ai_version": c.ai_version,
            "prompt_version": c.prompt_version,
            "status": c.status,
            "approval_reason": c.approval_reason,
            "approval_comment": c.approval_comment,
            "created_at": str(c.created_at),
        }
        for c in candidates
    ]


@router.post("/candidates/{candidate_id}/approve")
async def approve_candidate_endpoint(
    candidate_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    """Approve a knowledge candidate and ingest into KB."""
    from app.models.knowledge_base import KnowledgeCandidate, Document
    from app.services.knowledge_base import ingest_document
    from datetime import datetime

    # No tenant_id filter -- see list_candidates_endpoint's comment; this is
    # a global admin queue, and identity.tenant_id would've been an
    # AttributeError on require_admin's None return anyway.
    result = await db.execute(
        select(KnowledgeCandidate).where(KnowledgeCandidate.id == candidate_id).limit(1)
    )
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    # Update candidate status
    candidate.status = "approved"
    candidate.approval_reason = payload.get("reason", "other")
    candidate.approval_comment = payload.get("comment")
    candidate.reviewed_by = "admin"
    candidate.reviewed_at = datetime.utcnow()

    # Ingest into KB with versioning
    title = candidate.question[:80] + ("..." if len(candidate.question) > 80 else "")
    content = f"질문: {candidate.question}\n\n답변: {candidate.answer}"

    await ingest_document(
        db=db,
        title=title,
        content=content,
        source_type="ai_learned",
        source_url=f"candidate://{candidate.id}",
        collection="ai_learned",
        tenant_id=candidate.tenant_id,
    )

    await db.commit()
    return {"success": True, "message": "Candidate approved and ingested into KB"}


@router.post("/candidates/{candidate_id}/reject")
async def reject_candidate_endpoint(
    candidate_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    """Reject a knowledge candidate."""
    from app.models.knowledge_base import KnowledgeCandidate
    from datetime import datetime

    result = await db.execute(
        select(KnowledgeCandidate).where(KnowledgeCandidate.id == candidate_id).limit(1)
    )
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    candidate.status = "rejected"
    candidate.approval_reason = payload.get("reason", "other")
    candidate.approval_comment = payload.get("comment")
    candidate.reviewed_by = "admin"
    candidate.reviewed_at = datetime.utcnow()

    await db.commit()
    return {"success": True, "message": "Candidate rejected"}


#  AI Evolution Analytics


@router.get("/analytics/evolution")
async def evolution_analytics_endpoint(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    """Get AI evolution metrics for the admin dashboard."""
    from datetime import timedelta
    from sqlalchemy import func, case
    from app.models.knowledge_base import KnowledgeCandidate, Document

    now = utcnow_naive()
    since = now - timedelta(days=days)

    # Feedback accuracy (positive rate)
    feedback_query = select(
        func.count(AiChatMessageV2.id).label("total"),
        func.count(case((AiChatMessageV2.feedback_score >= 4, 1))).label("positive"),
        func.avg(AiChatMessageV2.feedback_score).label("avg_score"),
    ).where(
        AiChatMessageV2.role == "assistant",
        AiChatMessageV2.feedback_score.isnot(None),
        AiChatMessageV2.created_at >= since,
    )
    feedback_result = await db.execute(feedback_query)
    feedback_row = feedback_result.one()

    # Knowledge count
    kb_count_result = await db.execute(
        select(func.count(Document.id)).where(Document.is_published == True)
    )
    kb_count = kb_count_result.scalar() or 0

    # Candidate stats
    candidate_stats = await db.execute(
        select(
            func.count(KnowledgeCandidate.id).label("total"),
            func.count(case((KnowledgeCandidate.status == "pending", 1))).label("pending"),
            func.count(case((KnowledgeCandidate.status == "approved", 1))).label("approved"),
            func.count(case((KnowledgeCandidate.status == "rejected", 1))).label("rejected"),
        )
    )
    candidate_row = candidate_stats.one()

    # Learning speed (candidates per day)
    learning_speed = (candidate_row.approved or 0) / max(days, 1)

    # Accuracy
    total_feedback = feedback_row.total or 0
    positive_feedback = feedback_row.positive or 0
    accuracy = (positive_feedback / total_feedback * 100) if total_feedback > 0 else 0

    return {
        "accuracy": round(accuracy, 1),
        "satisfaction": round(float(feedback_row.avg_score or 0), 2),
        "knowledge_count": kb_count,
        "learning_speed": round(learning_speed, 1),
        "pending_candidates": candidate_row.pending or 0,
        "total_candidates": candidate_row.total or 0,
        "approved_candidates": candidate_row.approved or 0,
        "rejected_candidates": candidate_row.rejected or 0,
        "period_days": days,
    }


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

    # Save the tool result as an assistant message. For send_broadcast,
    # surface the real delivered/failed counts up front so the "completed"
    # message reflects what actually happened, not just that the tool ran.
    session_id = payload.get("session_id")
    if session_id:
        from app.models.ai_chat_v2 import AiChatMessageV2

        if tool_name == "send_broadcast" and isinstance(result.result, dict):
            delivered = result.result.get("delivered", 0)
            failed = result.result.get("failed", 0)
            summary_line = f"✅ 발송 완료 — 성공 {delivered}건 / 실패 {failed}건\n\n"
        else:
            summary_line = f"✅ {meta.get('label', tool_name)} 실행 완료\n\n"

        msg = AiChatMessageV2(
            id=str(uuid.uuid4()),
            session_id=session_id,
            tenant_id=identity.tenant_id,
            role="assistant",
            content=summary_line + json.dumps(result.result, ensure_ascii=False, default=str),
            model="tool",
        )
        db.add(msg)
        await db.commit()

    return {"success": True, "result": result.result}


#  File Upload for AI Chat


@router.post("/upload")
async def upload_file_for_ai_chat(
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Upload an image or video file for AI chat analysis.

    Returns the file URL and metadata. The AI can then analyze the file content.
    """
    import os
    import uuid

    # Validate file type
    allowed_types = [
        "image/jpeg", "image/png", "image/gif", "image/webp",
        "video/mp4", "video/webm", "video/ogg", "video/quicktime",
    ]
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"지원하지 않는 파일 형식입니다. 지원: {', '.join(allowed_types)}"
        )

    # Validate file size (max 20MB)
    max_size = 20 * 1024 * 1024  # 20MB
    contents = await file.read()
    if len(contents) > max_size:
        raise HTTPException(status_code=400, detail="파일 크기는 20MB 이하여야 합니다.")

    # Save file
    ext = file.filename.split(".")[-1] if file.filename and "." in file.filename else "bin"
    filename = f"{uuid.uuid4()}.{ext}"
    upload_dir = os.path.join("data", "uploads", "ai_chat")
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, filename)

    with open(filepath, "wb") as f:
        f.write(contents)

    # Return file info
    file_url = f"/uploads/ai_chat/{filename}"
    return {
        "url": file_url,
        "filename": file.filename or filename,
        "mime_type": file.content_type,
        "size": len(contents),
    }


#  Weekly AI Quality Report (user-facing)


@router.get("/analytics/quality-report")
async def ai_quality_report_endpoint(
    days: int = Query(default=7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Per-tenant AI quality summary — feedback, RAG effectiveness, learning."""
    from app.services.ai_chat_v2_service import get_ai_quality_report
    return await get_ai_quality_report(db, identity.tenant_id, days=days)


@router.get("/analytics/quality-admin")
async def ai_quality_admin_endpoint(
    days: int = Query(default=7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    """Admin AI quality analytics — quality trend, domain benchmark,
    low-quality improvement candidates (Q8/Q9/Q10)."""
    from app.services.ai_chat_v2_service import get_admin_quality_analytics
    return await get_admin_quality_analytics(db, days=days)