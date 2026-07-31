"""
TeleMon AI Platform API   AI  .

Features:
1. AI Chat  Graphiti   ,   ,  
2. AI Reply Assistant       , Graphiti  
3. AI Broadcast Assistant    AI ,   , A/B 
4. AI Operations Report   , /// ,  
5. AI Usage System   , Credits/ ,   
6. Admin AI  AI  ,  AI  , /,  
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Identity, get_current_identity, get_current_tenant_id, require_account_tenant_access, require_admin
from app.database import get_db
from app.core.logging import get_logger
from app.models.tenant import Tenant
from app.models.ai import (
    AiChatLog,
    AiReplyAssistantLog,
    AiBroadcastAssistantLog,
    AiOperationsReport,
    AiUsageRecord,
    AiPlanLimit,
)
from app.services.ai_core_service import (
    call_deepseek,
    call_llm_for_tenant,
    store_memory,
    search_memory,
    check_ai_quota,
    record_ai_usage,
    get_ai_usage_summary,
    AI_CHAT_SYSTEM_PROMPT,
    AI_REPLY_ASSISTANT_PROMPT,
    AI_BROADCAST_ASSISTANT_PROMPT,
    AI_OPERATIONS_REPORT_PROMPT,
    FEATURE_CHAT,
    FEATURE_REPLY_ASSISTANT,
    FEATURE_BROADCAST_ASSISTANT,
    FEATURE_OPERATIONS_REPORT,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/api/ai", tags=["ai"])


# 
# Pydantic Schemas
# 


class AiChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000, description=" ")
    session_id: str | None = Field(default=None, description="  ID (  )")
    use_memory: bool = Field(default=True, description="Graphiti    ")
    is_sensitive: bool = Field(default=False, description=" ")


class AiChatResponse(BaseModel):
    reply: str
    session_id: str
    tokens_used: int = 0
    memory_context: str = ""
    remaining_credits: int = 0


class AiChatHistoryItem(BaseModel):
    role: str
    content: str
    created_at: str


class AiChatHistoryResponse(BaseModel):
    session_id: str
    messages: list[AiChatHistoryItem]
    total: int


class AiReplyAssistantRequest(BaseModel):
    account_id: str = Field(..., description="  ID")
    chat_id: str = Field(..., description=" ID")
    chat_title: str | None = Field(default=None, description=" ")
    incoming_message: str = Field(..., min_length=1, max_length=2000, description=" ")
    use_memory: bool = Field(default=True, description="Graphiti   ")


class AiReplyAssistantResponse(BaseModel):
    suggested_reply: str
    confidence: float
    reason: str
    context_summary: str = ""


class AiBroadcastAssistantRequest(BaseModel):
    purpose: str = Field(..., min_length=1, max_length=500, description=" ")
    target_description: str | None = Field(default=None, description="  ( )")
    tone: str | None = Field(default="professional", description=": professional / friendly / urgent / promotional")
    language: str = Field(default="ko", description=" (ko/en/ja/zh)")
    generate_ab_test: bool = Field(default=True, description="A/B    ")


class AiBroadcastAssistantResponse(BaseModel):
    message: str
    variant_a: str | None = None
    variant_b: str | None = None
    reasoning: str = ""


class AiOperationsReportRequest(BaseModel):
    report_type: str = Field(default="daily", description=" : daily / weekly / custom")
    days: int = Field(default=1, ge=1, le=90, description=" ()")
    include_recommendations: bool = Field(default=True, description="   ")


class AiOperationsReportResponse(BaseModel):
    report_id: str
    report_type: str
    period_start: str
    period_end: str
    summary: str
    sections: list[dict] = []
    insights: list[dict] = []
    recommendations: list[dict] = []
    metrics: dict[str, Any] = {}
    created_at: str


class AiUsageSummaryResponse(BaseModel):
    features: dict[str, Any] = {}
    total_requests: int = 0
    total_tokens: int = 0
    total_credits: float = 0.0
    period_days: int = 30


class AiPlanLimitResponse(BaseModel):
    id: str
    plan: str
    feature: str
    max_requests_per_day: int
    max_tokens_per_day: int
    max_credits_per_month: float
    is_enabled: bool


class AiPlanLimitUpdateRequest(BaseModel):
    max_requests_per_day: int | None = None
    max_tokens_per_day: int | None = None
    max_credits_per_month: float | None = None
    is_enabled: bool | None = None


class AiAdminLogQuery(BaseModel):
    feature: str | None = Field(default=None, description="AI  ")
    tenant_id: str | None = Field(default=None, description=" ID ")
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
    start_date: str | None = None
    end_date: str | None = None


class AiAdminLogResponse(BaseModel):
    items: list[dict] = []
    total: int = 0
    limit: int = 50
    offset: int = 0


# 
# 1. AI Chat
# 


@router.post("/chat", response_model=AiChatResponse)
async def ai_chat(
    payload: AiChatRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> AiChatResponse:
    """AI Chat  Graphiti   ,   ."""
    session_id = payload.session_id or str(uuid.uuid4())

    # Load tenant for credit check
    tenant_row = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = tenant_row.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="테넌트를 찾을 수 없습니다.")

    # Credit check (server-enforced)
    from app.services.ai_credit_service import check_and_deduct_credits
    ok, remaining = await check_and_deduct_credits(tenant, db, FEATURE_CHAT, payload.is_sensitive)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"AI 크레딧이 부족합니다. (남은 크레딧: {remaining})",
        )

    # Check quota (legacy plan-limit enforcement)
    allowed, reason = await check_ai_quota(db, tenant_id, FEATURE_CHAT)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=reason)

    # Build context from memory
    memory_context = ""
    if payload.use_memory:
        memory_results = await search_memory(tenant_id, payload.message)
        if memory_results:
            memory_context = "\n".join(
                f"- {r.get('content', '')}" for r in memory_results[:3]
            )

    # Get recent chat history
    history_result = await db.execute(
        select(AiChatLog)
        .where(
            AiChatLog.tenant_id == tenant_id,
            AiChatLog.session_id == session_id,
        )
        .order_by(AiChatLog.created_at.asc())
        .limit(20)
    )
    history = history_result.scalars().all()

    # Build messages for DeepSeek
    system_prompt = AI_CHAT_SYSTEM_PROMPT
    if memory_context:
        system_prompt += f"\n\n[  ]\n{memory_context}"

    messages = [{"role": "system", "content": system_prompt}]
    for msg in history:
        messages.append({"role": msg.role, "content": msg.content})
    messages.append({"role": "user", "content": payload.message})

    # Free-plan tenants route to the local Ollama model instead of DeepSeek
    # (falls back to DeepSeek automatically if Ollama is unreachable).
    tenant_plan_row = await db.execute(select(Tenant.plan).where(Tenant.id == tenant_id))
    tenant_plan = tenant_plan_row.scalar_one_or_none() or "free"
    reply, tokens_used, _ = await call_llm_for_tenant(tenant_plan, messages)
    if reply is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI   .    .",
        )

    # Save to chat logs
    user_log = AiChatLog(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        session_id=session_id,
        role="user",
        content=payload.message,
        tokens_used=0,
    )
    assistant_log = AiChatLog(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        session_id=session_id,
        role="assistant",
        content=reply,
        tokens_used=tokens_used,
    )
    db.add(user_log)
    db.add(assistant_log)

    # Record usage
    await record_ai_usage(db, tenant_id, FEATURE_CHAT, tokens_used=tokens_used)

    # Store in Graphiti memory
    if payload.use_memory:
        await store_memory(
            tenant_id=tenant_id,
            name=f"ai_chat_{session_id[:8]}",
            episode_body=json.dumps({
                "user_message": payload.message,
                "assistant_reply": reply[:500],
                "session_id": session_id,
            }, ensure_ascii=False),
            source="text",
            source_description="AI Chat conversation",
        )

    await db.commit()

    return AiChatResponse(
        reply=reply,
        session_id=session_id,
        tokens_used=tokens_used,
        memory_context=memory_context[:500] if memory_context else "",
        remaining_credits=remaining,
    )


@router.get("/chat/history/{session_id}", response_model=AiChatHistoryResponse)
async def get_chat_history(
    session_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> AiChatHistoryResponse:
    """Get chat history for a specific session."""
    result = await db.execute(
        select(AiChatLog)
        .where(
            AiChatLog.tenant_id == tenant_id,
            AiChatLog.session_id == session_id,
        )
        .order_by(AiChatLog.created_at.asc())
    )
    messages = result.scalars().all()

    return AiChatHistoryResponse(
        session_id=session_id,
        messages=[
            AiChatHistoryItem(
                role=msg.role,
                content=msg.content,
                created_at=msg.created_at.isoformat() if msg.created_at else "",
            )
            for msg in messages
        ],
        total=len(messages),
    )


@router.get("/chat/sessions", response_model=list[dict])
async def list_chat_sessions(
    limit: int = Query(default=20, ge=1, le=100),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List all chat sessions for the current tenant."""
    result = await db.execute(
        select(
            AiChatLog.session_id,
            func.min(AiChatLog.created_at).label("first_message"),
            func.max(AiChatLog.created_at).label("last_message"),
            func.count(AiChatLog.id).label("message_count"),
        )
        .where(AiChatLog.tenant_id == tenant_id)
        .group_by(AiChatLog.session_id)
        .order_by(desc("last_message"))
        .limit(limit)
    )
    sessions = result.all()

    return [
        {
            "session_id": s.session_id,
            "first_message": s.first_message.isoformat() if s.first_message else "",
            "last_message": s.last_message.isoformat() if s.last_message else "",
            "message_count": s.message_count,
        }
        for s in sessions
    ]


# 
# 2. AI Reply Assistant
# 


@router.post("/reply-assistant", response_model=AiReplyAssistantResponse)
async def ai_reply_assistant(
    payload: AiReplyAssistantRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> AiReplyAssistantResponse:
    """AI Reply Assistant       ."""
    tenant_id = identity.tenant_id

    # Check quota
    allowed, reason = await check_ai_quota(db, tenant_id, FEATURE_REPLY_ASSISTANT)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=reason)

    # Build context from memory
    memory_context = ""
    if payload.use_memory:
        memory_results = await search_memory(
            tenant_id,
            f"reply context for chat {payload.chat_title or payload.chat_id}",
        )
        if memory_results:
            memory_context = "\n".join(
                f"- {r.get('content', '')}" for r in memory_results[:3]
            )

    # Build system prompt
    system_prompt = AI_REPLY_ASSISTANT_PROMPT
    if memory_context:
        system_prompt += f"\n\n[  ]\n{memory_context}"
    if payload.chat_title:
        system_prompt += f"\n\n[]\n{payload.chat_title}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"    :\n\n{payload.incoming_message}"},
    ]

    reply, tokens_used, _ = await call_deepseek(messages, max_tokens=500)
    if reply is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI   .",
        )

    # Parse JSON response
    try:
        parsed = json.loads(reply.strip())
        suggested_reply = parsed.get("reply", reply)
        confidence = min(max(float(parsed.get("confidence", 0.5)), 0.0), 1.0)
        reason = parsed.get("reason", "")
    except (json.JSONDecodeError, TypeError, ValueError):
        suggested_reply = reply
        confidence = 0.5
        reason = "AI  "

    # Save log
    log_entry = AiReplyAssistantLog(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        account_id=payload.account_id,
        chat_id=payload.chat_id,
        chat_title=payload.chat_title,
        incoming_message=payload.incoming_message,
        suggested_reply=suggested_reply,
        confidence=confidence,
        context_summary=memory_context[:500] if memory_context else "",
    )
    db.add(log_entry)

    # Record usage
    await record_ai_usage(db, tenant_id, FEATURE_REPLY_ASSISTANT, tokens_used=tokens_used)

    # Store in Graphiti memory
    if payload.use_memory:
        await store_memory(
            tenant_id=tenant_id,
            name=f"reply_{payload.chat_id[:8]}",
            episode_body=json.dumps({
                "chat_id": payload.chat_id,
                "chat_title": payload.chat_title,
                "incoming": payload.incoming_message[:200],
                "suggested": suggested_reply[:200],
            }, ensure_ascii=False),
            source="text",
            source_description="AI Reply Assistant suggestion",
        )

    await db.commit()

    return AiReplyAssistantResponse(
        suggested_reply=suggested_reply,
        confidence=confidence,
        reason=reason,
        context_summary=memory_context[:500] if memory_context else "",
    )


@router.post("/reply-assistant/{log_id}/send")
async def mark_reply_sent(
    log_id: str,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Mark a reply assistant suggestion as sent."""
    result = await db.execute(
        select(AiReplyAssistantLog).where(
            AiReplyAssistantLog.id == log_id,
            AiReplyAssistantLog.tenant_id == identity.tenant_id,
        )
    )
    log_entry = result.scalar_one_or_none()
    if not log_entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Log entry not found")

    log_entry.was_sent = True
    log_entry.sent_at = datetime.now(timezone.utc)
    await db.commit()

    return {"status": "ok", "log_id": log_id}


# 
# 3. AI Broadcast Assistant
# 


@router.post("/broadcast-assistant", response_model=AiBroadcastAssistantResponse)
async def ai_broadcast_assistant(
    payload: AiBroadcastAssistantRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> AiBroadcastAssistantResponse:
    """AI Broadcast Assistant    AI , A/B ."""
    tenant_id = identity.tenant_id

    # Check quota
    allowed, reason = await check_ai_quota(db, tenant_id, FEATURE_BROADCAST_ASSISTANT)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=reason)

    # Build system prompt
    system_prompt = AI_BROADCAST_ASSISTANT_PROMPT
    system_prompt += f"\n\n: {payload.tone or 'professional'}"
    system_prompt += f"\n: {payload.language or 'ko'}"
    if payload.generate_ab_test:
        system_prompt += "\n\nA/B  variant_a variant_b    ."

    user_prompt = f" : {payload.purpose}"
    if payload.target_description:
        user_prompt += f"\n: {payload.target_description}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    reply, tokens_used, _ = await call_deepseek(messages, max_tokens=800)
    if reply is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI   .",
        )

    # Parse JSON response
    try:
        parsed = json.loads(reply.strip())
        message = parsed.get("message", reply)
        variant_a = parsed.get("variant_a")
        variant_b = parsed.get("variant_b")
        reasoning = parsed.get("reasoning", "")
    except (json.JSONDecodeError, TypeError, ValueError):
        message = reply
        variant_a = None
        variant_b = None
        reasoning = ""

    # Save log
    log_entry = AiBroadcastAssistantLog(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        purpose=payload.purpose,
        target_description=payload.target_description,
        generated_message=message,
        variant_a=variant_a,
        variant_b=variant_b,
        tone=payload.tone,
        language=payload.language,
    )
    db.add(log_entry)

    # Record usage
    await record_ai_usage(db, tenant_id, FEATURE_BROADCAST_ASSISTANT, tokens_used=tokens_used)

    await db.commit()

    return AiBroadcastAssistantResponse(
        message=message,
        variant_a=variant_a,
        variant_b=variant_b,
        reasoning=reasoning,
    )


# 
# 4. AI Operations Report
# 


@router.post("/operations-report", response_model=AiOperationsReportResponse)
async def ai_operations_report(
    payload: AiOperationsReportRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> AiOperationsReportResponse:
    """AI Operations Report   , ,  ."""
    tenant_id = identity.tenant_id

    # Check quota
    allowed, reason = await check_ai_quota(db, tenant_id, FEATURE_OPERATIONS_REPORT)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=reason)

    period_end = datetime.now(timezone.utc)
    period_start = period_end - timedelta(days=payload.days)

    # Gather operational data
    metrics = await _gather_operations_metrics(db, tenant_id, payload.days)

    # Build system prompt
    system_prompt = AI_OPERATIONS_REPORT_PROMPT
    if payload.include_recommendations:
        system_prompt += "\n\n   ."

    user_prompt = (
        f" : {period_start.strftime('%Y-%m-%d')} ~ {period_end.strftime('%Y-%m-%d')}\n\n"
        f"[ ]\n{json.dumps(metrics, ensure_ascii=False, indent=2)}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    reply, tokens_used, _ = await call_deepseek(messages, max_tokens=1500)
    if reply is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI   .",
        )

    # Parse JSON response
    try:
        parsed = json.loads(reply.strip())
        summary = parsed.get("summary", reply[:500])
        sections = parsed.get("sections", [])
        insights = parsed.get("insights", [])
        recommendations = parsed.get("recommendations", [])
    except (json.JSONDecodeError, TypeError, ValueError):
        summary = reply[:1000]
        sections = []
        insights = []
        recommendations = []

    # Save report
    report = AiOperationsReport(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        report_type=payload.report_type,
        period_start=period_start,
        period_end=period_end,
        summary=summary,
        sections=sections,
        insights=insights,
        recommendations=recommendations,
        metrics=metrics,
        tokens_used=tokens_used,
    )
    db.add(report)

    # Record usage
    await record_ai_usage(db, tenant_id, FEATURE_OPERATIONS_REPORT, tokens_used=tokens_used)

    await db.commit()

    return AiOperationsReportResponse(
        report_id=report.id,
        report_type=payload.report_type,
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
        summary=summary,
        sections=sections,
        insights=insights,
        recommendations=recommendations,
        metrics=metrics,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/operations-reports", response_model=list[dict])
async def list_operations_reports(
    limit: int = Query(default=10, ge=1, le=50),
    report_type: str | None = Query(default=None, description="  "),
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List AI operations reports for the current tenant."""
    query = select(AiOperationsReport).where(
        AiOperationsReport.tenant_id == identity.tenant_id
    )
    if report_type:
        query = query.where(AiOperationsReport.report_type == report_type)
    query = query.order_by(AiOperationsReport.created_at.desc()).limit(limit)

    result = await db.execute(query)
    reports = result.scalars().all()

    return [
        {
            "id": r.id,
            "report_type": r.report_type,
            "period_start": r.period_start.isoformat() if r.period_start else "",
            "period_end": r.period_end.isoformat() if r.period_end else "",
            "summary": r.summary[:200],
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in reports
    ]


@router.get("/operations-reports/{report_id}", response_model=AiOperationsReportResponse)
async def get_operations_report(
    report_id: str,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> AiOperationsReportResponse:
    """Get a specific operations report."""
    result = await db.execute(
        select(AiOperationsReport).where(
            AiOperationsReport.id == report_id,
            AiOperationsReport.tenant_id == identity.tenant_id,
        )
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")

    return AiOperationsReportResponse(
        report_id=report.id,
        report_type=report.report_type,
        period_start=report.period_start.isoformat() if report.period_start else "",
        period_end=report.period_end.isoformat() if report.period_end else "",
        summary=report.summary,
        sections=report.sections or [],
        insights=report.insights or [],
        recommendations=report.recommendations or [],
        metrics=report.metrics or {},
        created_at=report.created_at.isoformat() if report.created_at else "",
    )


# 
# 5. AI Usage System
# 


@router.get("/usage", response_model=AiUsageSummaryResponse)
async def get_ai_usage(
    days: int = Query(default=30, ge=1, le=90),
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> AiUsageSummaryResponse:
    """Get AI usage summary for the current tenant."""
    summary = await get_ai_usage_summary(db, identity.tenant_id, days=days)
    return AiUsageSummaryResponse(**summary)


@router.get("/credits")
async def get_ai_credits(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get current AI credit balance for the tenant."""
    tenant_row = await db.execute(select(Tenant).where(Tenant.id == identity.tenant_id))
    tenant = tenant_row.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    from app.services.ai_credit_service import get_remaining_credits
    remaining = await get_remaining_credits(tenant)
    return {
        "remaining_credits": remaining,
        "reset_tokens": tenant.ai_credits_reset_tokens,
        "plan": tenant.plan,
        "last_refill_at": tenant.ai_last_refill_at.isoformat() if tenant.ai_last_refill_at else None,
    }


@router.post("/credits/reset")
async def reset_ai_credits(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Use one reset token to refill Pro credits to 100,000."""
    tenant_row = await db.execute(select(Tenant).where(Tenant.id == identity.tenant_id))
    tenant = tenant_row.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    from app.services.ai_credit_service import reset_credits
    ok, remaining = await reset_credits(tenant, db)
    if not ok:
        raise HTTPException(status_code=400, detail="No reset tokens remaining or not a Pro plan.")
    return {"remaining_credits": remaining, "reset_tokens": tenant.ai_credits_reset_tokens}


@router.get("/plan-limits", response_model=list[AiPlanLimitResponse])
async def get_plan_limits(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> list[AiPlanLimitResponse]:
    """Get AI plan limits for the current tenant's plan."""
    # Get the tenant's plan from the identity
    tenant_plan = getattr(identity, "plan", "free")

    result = await db.execute(
        select(AiPlanLimit).where(AiPlanLimit.plan == tenant_plan)
    )
    limits = result.scalars().all()

    return [
        AiPlanLimitResponse(
            id=limit.id,
            plan=limit.plan,
            feature=limit.feature,
            max_requests_per_day=limit.max_requests_per_day,
            max_tokens_per_day=limit.max_tokens_per_day,
            max_credits_per_month=limit.max_credits_per_month,
            is_enabled=limit.is_enabled,
        )
        for limit in limits
    ]


# 
# 6. Admin AI
# 


@router.get("/admin/logs", response_model=AiAdminLogResponse, dependencies=[Depends(require_admin)])
async def get_ai_admin_logs(
    feature: str | None = Query(default=None, description="AI  "),
    tenant_id: str | None = Query(default=None, description=" ID "),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> AiAdminLogResponse:
    """Admin: Get all AI logs with search/filter."""
    items = []
    total = 0

    # Query chat logs
    if feature is None or feature == "chat":
        query = select(AiChatLog)
        count_query = select(func.count(AiChatLog.id))
        if tenant_id:
            query = query.where(AiChatLog.tenant_id == tenant_id)
            count_query = count_query.where(AiChatLog.tenant_id == tenant_id)
        if start_date:
            query = query.where(AiChatLog.created_at >= start_date)
        if end_date:
            query = query.where(AiChatLog.created_at <= end_date)
        query = query.order_by(AiChatLog.created_at.desc()).limit(limit).offset(offset)

        result = await db.execute(query)
        logs = result.scalars().all()
        count_result = await db.execute(count_query)
        total += count_result.scalar() or 0

        for log in logs:
            items.append({
                "id": log.id,
                "type": "chat",
                "tenant_id": log.tenant_id,
                "session_id": log.session_id,
                "role": log.role,
                "content": log.content[:500],
                "tokens_used": log.tokens_used,
                "created_at": log.created_at.isoformat() if log.created_at else "",
            })

    # Query reply assistant logs
    if feature is None or feature == "reply_assistant":
        query = select(AiReplyAssistantLog)
        count_query = select(func.count(AiReplyAssistantLog.id))
        if tenant_id:
            query = query.where(AiReplyAssistantLog.tenant_id == tenant_id)
            count_query = count_query.where(AiReplyAssistantLog.tenant_id == tenant_id)
        if start_date:
            query = query.where(AiReplyAssistantLog.created_at >= start_date)
        if end_date:
            query = query.where(AiReplyAssistantLog.created_at <= end_date)
        query = query.order_by(AiReplyAssistantLog.created_at.desc()).limit(limit).offset(offset)

        result = await db.execute(query)
        logs = result.scalars().all()
        count_result = await db.execute(count_query)
        total += count_result.scalar() or 0

        for log in logs:
            items.append({
                "id": log.id,
                "type": "reply_assistant",
                "tenant_id": log.tenant_id,
                "account_id": log.account_id,
                "chat_title": log.chat_title,
                "incoming_message": log.incoming_message[:200],
                "suggested_reply": log.suggested_reply[:200],
                "confidence": log.confidence,
                "was_sent": log.was_sent,
                "created_at": log.created_at.isoformat() if log.created_at else "",
            })

    # Query broadcast assistant logs
    if feature is None or feature == "broadcast_assistant":
        query = select(AiBroadcastAssistantLog)
        count_query = select(func.count(AiBroadcastAssistantLog.id))
        if tenant_id:
            query = query.where(AiBroadcastAssistantLog.tenant_id == tenant_id)
            count_query = count_query.where(AiBroadcastAssistantLog.tenant_id == tenant_id)
        if start_date:
            query = query.where(AiBroadcastAssistantLog.created_at >= start_date)
        if end_date:
            query = query.where(AiBroadcastAssistantLog.created_at <= end_date)
        query = query.order_by(AiBroadcastAssistantLog.created_at.desc()).limit(limit).offset(offset)

        result = await db.execute(query)
        logs = result.scalars().all()
        count_result = await db.execute(count_query)
        total += count_result.scalar() or 0

        for log in logs:
            items.append({
                "id": log.id,
                "type": "broadcast_assistant",
                "tenant_id": log.tenant_id,
                "purpose": log.purpose,
                "generated_message": log.generated_message[:300],
                "tone": log.tone,
                "language": log.language,
                "was_sent": log.was_sent,
                "created_at": log.created_at.isoformat() if log.created_at else "",
            })

    # Sort by created_at desc
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    return AiAdminLogResponse(
        items=items[:limit],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/admin/logs/{tenant_id}/summary", dependencies=[Depends(require_admin)])
async def get_tenant_ai_summary(
    tenant_id: str,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Admin: Get AI usage summary for a specific tenant."""
    # Get usage summary
    usage = await get_ai_usage_summary(db, tenant_id, days=30)

    # Get chat session count
    session_result = await db.execute(
        select(func.count(func.distinct(AiChatLog.session_id)))
        .where(AiChatLog.tenant_id == tenant_id)
    )
    session_count = session_result.scalar() or 0

    # Get reply assistant count
    reply_result = await db.execute(
        select(func.count(AiReplyAssistantLog.id))
        .where(AiReplyAssistantLog.tenant_id == tenant_id)
    )
    reply_count = reply_result.scalar() or 0

    # Get broadcast assistant count
    broadcast_result = await db.execute(
        select(func.count(AiBroadcastAssistantLog.id))
        .where(AiBroadcastAssistantLog.tenant_id == tenant_id)
    )
    broadcast_count = broadcast_result.scalar() or 0

    # Get report count
    report_result = await db.execute(
        select(func.count(AiOperationsReport.id))
        .where(AiOperationsReport.tenant_id == tenant_id)
    )
    report_count = report_result.scalar() or 0

    return {
        "tenant_id": tenant_id,
        "usage": usage,
        "chat_sessions": session_count,
        "reply_assistant_uses": reply_count,
        "broadcast_assistant_uses": broadcast_count,
        "operations_reports": report_count,
    }


# 
# Admin: AI Plan Limits Management
# 


@router.get("/admin/plan-limits", response_model=list[AiPlanLimitResponse], dependencies=[Depends(require_admin)])
async def admin_list_plan_limits(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> list[AiPlanLimitResponse]:
    """Admin: List all AI plan limits."""
    result = await db.execute(select(AiPlanLimit).order_by(AiPlanLimit.plan, AiPlanLimit.feature))
    limits = result.scalars().all()

    return [
        AiPlanLimitResponse(
            id=limit.id,
            plan=limit.plan,
            feature=limit.feature,
            max_requests_per_day=limit.max_requests_per_day,
            max_tokens_per_day=limit.max_tokens_per_day,
            max_credits_per_month=limit.max_credits_per_month,
            is_enabled=limit.is_enabled,
        )
        for limit in limits
    ]


@router.put("/admin/plan-limits/{limit_id}", response_model=AiPlanLimitResponse, dependencies=[Depends(require_admin)])
async def admin_update_plan_limit(
    limit_id: str,
    payload: AiPlanLimitUpdateRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> AiPlanLimitResponse:
    """Admin: Update an AI plan limit."""
    result = await db.execute(select(AiPlanLimit).where(AiPlanLimit.id == limit_id))
    limit = result.scalar_one_or_none()
    if not limit:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan limit not found")

    if payload.max_requests_per_day is not None:
        limit.max_requests_per_day = payload.max_requests_per_day
    if payload.max_tokens_per_day is not None:
        limit.max_tokens_per_day = payload.max_tokens_per_day
    if payload.max_credits_per_month is not None:
        limit.max_credits_per_month = payload.max_credits_per_month
    if payload.is_enabled is not None:
        limit.is_enabled = payload.is_enabled

    await db.commit()

    return AiPlanLimitResponse(
        id=limit.id,
        plan=limit.plan,
        feature=limit.feature,
        max_requests_per_day=limit.max_requests_per_day,
        max_tokens_per_day=limit.max_tokens_per_day,
        max_credits_per_month=limit.max_credits_per_month,
        is_enabled=limit.is_enabled,
    )


@router.post("/admin/plan-limits/seed", dependencies=[Depends(require_admin)])
async def admin_seed_plan_limits(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Admin: Seed default AI plan limits for all plans."""
    default_limits = [
        # Free plan — 5h/10 AI chat refill (tracked on frontend)
        {"plan": "free", "feature": "chat", "max_requests_per_day": 10, "max_tokens_per_day": 5000, "max_credits_per_month": 0, "is_enabled": True},
        {"plan": "free", "feature": "reply_assistant", "max_requests_per_day": 20, "max_tokens_per_day": 10000, "max_credits_per_month": 0, "is_enabled": True},
        {"plan": "free", "feature": "broadcast_assistant", "max_requests_per_day": 5, "max_tokens_per_day": 5000, "max_credits_per_month": 0, "is_enabled": True},
        {"plan": "free", "feature": "operations_report", "max_requests_per_day": 2, "max_tokens_per_day": 10000, "max_credits_per_month": 0, "is_enabled": True},
        # Pro plan — $99/month, 100K AI credits
        {"plan": "pro", "feature": "chat", "max_requests_per_day": 500, "max_tokens_per_day": 250000, "max_credits_per_month": 100000, "is_enabled": True},
        {"plan": "pro", "feature": "reply_assistant", "max_requests_per_day": 500, "max_tokens_per_day": 250000, "max_credits_per_month": 100000, "is_enabled": True},
        {"plan": "pro", "feature": "broadcast_assistant", "max_requests_per_day": 200, "max_tokens_per_day": 100000, "max_credits_per_month": 100000, "is_enabled": True},
        {"plan": "pro", "feature": "operations_report", "max_requests_per_day": 50, "max_tokens_per_day": 200000, "max_credits_per_month": 100000, "is_enabled": True},
    ]

    created = 0
    for limit_data in default_limits:
        # Check if already exists
        existing = await db.execute(
            select(AiPlanLimit).where(
                AiPlanLimit.plan == limit_data["plan"],
                AiPlanLimit.feature == limit_data["feature"],
            )
        )
        if existing.scalar_one_or_none():
            continue

        limit = AiPlanLimit(
            id=str(uuid.uuid4()),
            **limit_data,
        )
        db.add(limit)
        created += 1

    await db.commit()
    return {"status": "ok", "created": created}


# 
# Internal Helpers
# 


async def _gather_operations_metrics(
    db: AsyncSession,
    tenant_id: str,
    days: int,
) -> dict[str, Any]:
    """Gather operational metrics for AI report generation."""
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    metrics: dict[str, Any] = {}

    # Message delivery stats
    try:
        from app.models.message_log import MessageLog
        msg_result = await db.execute(
            select(
                func.count(MessageLog.id).label("total"),
                func.sum(func.case((MessageLog.status == "sent", 1), else_=0)).label("sent"),
                func.sum(func.case((MessageLog.status == "failed", 1), else_=0)).label("failed"),
            ).where(
                MessageLog.tenant_id == tenant_id,
                MessageLog.created_at >= since,
            )
        )
        row = msg_result.one()
        metrics["messages"] = {
            "total": row.total or 0,
            "sent": row.sent or 0,
            "failed": row.failed or 0,
        }
    except Exception as exc:
        logger.warning("failed_to_gather_message_metrics", error=str(exc))
        metrics["messages"] = {"total": 0, "sent": 0, "failed": 0}

    # AI usage stats
    try:
        ai_usage = await get_ai_usage_summary(db, tenant_id, days=days)
        metrics["ai_usage"] = ai_usage
    except Exception as exc:
        logger.warning("failed_to_gather_ai_usage", error=str(exc))
        metrics["ai_usage"] = {}

    # Reply assistant stats
    try:
        reply_result = await db.execute(
            select(
                func.count(AiReplyAssistantLog.id).label("total"),
                func.sum(func.case((AiReplyAssistantLog.was_sent == True, 1), else_=0)).label("sent"),
            ).where(
                AiReplyAssistantLog.tenant_id == tenant_id,
                AiReplyAssistantLog.created_at >= since,
            )
        )
        row = reply_result.one()
        metrics["reply_assistant"] = {
            "total_suggestions": row.total or 0,
            "sent_replies": row.sent or 0,
        }
    except Exception as exc:
        logger.warning("failed_to_gather_reply_metrics", error=str(exc))
        metrics["reply_assistant"] = {"total_suggestions": 0, "sent_replies": 0}

    # Broadcast assistant stats
    try:
        broadcast_result = await db.execute(
            select(func.count(AiBroadcastAssistantLog.id)).where(
                AiBroadcastAssistantLog.tenant_id == tenant_id,
                AiBroadcastAssistantLog.created_at >= since,
            )
        )
        metrics["broadcast_assistant"] = {
            "total_generations": broadcast_result.scalar() or 0,
        }
    except Exception as exc:
        logger.warning("failed_to_gather_broadcast_metrics", error=str(exc))
        metrics["broadcast_assistant"] = {"total_generations": 0}

    return metrics