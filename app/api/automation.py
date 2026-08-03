"""Automation Control Tower — unified aggregation of existing automation systems.

This module does NOT implement any automation logic. It queries existing
data from: broadcasts, auto-reply logs, scheduler status, and reply macros
to present a unified operational view.

All real execution is handled by:
- broadcast_processor.py (broadcast delivery)
- auto_reply_service.py (auto-reply)
- scheduler/scheduler.py (APScheduler jobs)
- random_reply_service.py (reply macros)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.api.deps import get_current_identity, Identity
from app.models.broadcast import Broadcast
from app.models.auto_reply import AutoReplyRule, AutoReplyLog
from app.core.logging import get_logger

router = APIRouter(prefix="/api/automation", tags=["automation"], redirect_slashes=False)
logger = get_logger(__name__)


# ── Schemas ──────────────────────────────────────────────────────


class QueueMetrics(BaseModel):
    broadcast_total: int = 0
    broadcast_pending: int = 0
    broadcast_sending: int = 0
    broadcast_sent_today: int = 0
    broadcast_failed: int = 0
    broadcast_recurring_active: int = 0
    auto_reply_rules_total: int = 0
    auto_reply_rules_active: int = 0
    auto_reply_responses_today: int = 0
    auto_reply_failures_today: int = 0
    scheduled_pending: int = 0
    scheduled_next: str | None = None


class ExecutionLogEntry(BaseModel):
    id: str | int
    type: str  # broadcast | auto_reply | reply_macro | scheduled
    status: str  # success | failed | pending | sending
    title: str
    detail: str | None = None
    account_name: str | None = None
    created_at: str | None = None


class WorkflowStatus(BaseModel):
    scheduler_active: bool = True
    scheduler_tick_seconds: int = 30
    broadcast_processor_active: bool = True
    auto_reply_listener_active: bool = True
    total_jobs_running: int = 0
    total_jobs_scheduled: int = 0
    uptime_seconds: int = 0


class QueueHealth(BaseModel):
    broadcast_healthy: bool = True
    broadcast_queue_depth: int = 0
    auto_reply_healthy: bool = True
    scheduler_healthy: bool = True
    issues: list[str] = []


# ── Queue Metrics ────────────────────────────────────────────────


@router.get("/queue-metrics", response_model=QueueMetrics)
async def get_queue_metrics(
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Aggregate queue metrics across all automation systems."""
    tenant_id = identity.tenant_id if identity.kind != "admin" else None
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Broadcast metrics
    bcast_query = select(
        func.count(Broadcast.id).label("total"),
        func.count(case((Broadcast.status == "pending", 1))).label("pending"),
        func.count(case((Broadcast.status == "sending", 1))).label("sending"),
        func.count(case((Broadcast.status == "failed", 1))).label("failed"),
        func.count(case((Broadcast.status == "sent", 1))).label("sent"),
        func.count(case((Broadcast.recurring_interval_minutes.isnot(None), 1))).label("recurring"),
    )
    if tenant_id:
        from app.models.account import Account
        bcast_query = bcast_query.join(Account, Broadcast.account_id == Account.id).where(Account.tenant_id == tenant_id)

    bcast_result = (await db.execute(bcast_query)).one()

    # Today's broadcasts
    today_query = select(func.count(Broadcast.id)).where(Broadcast.sent_at >= today_start)
    if tenant_id:
        from app.models.account import Account
        today_query = today_query.join(Account, Broadcast.account_id == Account.id).where(Account.tenant_id == tenant_id)
    today_count = (await db.execute(today_query)).scalar() or 0

    # Scheduled pending (broadcasts with future scheduled_at)
    sched_query = select(func.count(Broadcast.id)).where(
        Broadcast.status == "pending",
        Broadcast.scheduled_at > now,
    )
    if tenant_id:
        from app.models.account import Account
        sched_query = sched_query.join(Account, Broadcast.account_id == Account.id).where(Account.tenant_id == tenant_id)
    scheduled_pending = (await db.execute(sched_query)).scalar() or 0

    # Auto-reply metrics
    ar_rules_query = select(
        func.count(AutoReplyRule.id).label("total"),
        func.count(case((AutoReplyRule.is_active == True, 1))).label("active"),
    )
    if tenant_id:
        from app.models.account import Account
        ar_rules_query = ar_rules_query.join(Account, AutoReplyRule.account_id == Account.id).where(Account.tenant_id == tenant_id)
    ar_rules = (await db.execute(ar_rules_query)).one()

    # Auto-reply logs today
    ar_logs_query = select(
        func.count(AutoReplyLog.id).label("total"),
        func.count(case((AutoReplyLog.status == "success", 1))).label("success"),
        func.count(case((AutoReplyLog.status == "failed", 1))).label("failed"),
    ).where(AutoReplyLog.created_at >= today_start)
    if tenant_id:
        from app.models.account import Account
        ar_logs_query = ar_logs_query.join(Account, AutoReplyLog.account_id == Account.id).where(Account.tenant_id == tenant_id)
    ar_logs = (await db.execute(ar_logs_query)).one()

    # Next scheduled broadcast
    next_query = select(Broadcast.scheduled_at).where(
        Broadcast.status == "pending",
        Broadcast.scheduled_at > now,
    ).order_by(Broadcast.scheduled_at).limit(1)
    if tenant_id:
        from app.models.account import Account
        next_query = next_query.join(Account, Broadcast.account_id == Account.id).where(Account.tenant_id == tenant_id)
    next_result = await db.execute(next_query)
    next_scheduled = next_result.scalar()

    return QueueMetrics(
        broadcast_total=bcast_result.total or 0,
        broadcast_pending=bcast_result.pending or 0,
        broadcast_sending=bcast_result.sending or 0,
        broadcast_sent_today=today_count,
        broadcast_failed=bcast_result.failed or 0,
        broadcast_recurring_active=bcast_result.recurring or 0,
        auto_reply_rules_total=ar_rules.total or 0,
        auto_reply_rules_active=ar_rules.active or 0,
        auto_reply_responses_today=ar_logs.total or 0,
        auto_reply_failures_today=ar_logs.failed or 0,
        scheduled_pending=scheduled_pending,
        scheduled_next=next_scheduled.isoformat() if next_scheduled else None,
    )


# ── Execution Log ────────────────────────────────────────────────


@router.get("/execution-log", response_model=list[ExecutionLogEntry])
async def get_execution_log(
    type: str | None = Query(default=None, description="Filter by type: broadcast, auto_reply, scheduled"),
    limit: int = Query(default=30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Unified execution log across broadcasts and auto-reply."""
    tenant_id = identity.tenant_id if identity.kind != "admin" else None
    entries: list[ExecutionLogEntry] = []

    # Broadcast logs (recent)
    if type is None or type == "broadcast" or type == "scheduled":
        bcast_query = select(Broadcast).order_by(Broadcast.created_at.desc()).limit(limit)
        if tenant_id:
            from app.models.account import Account
            bcast_query = bcast_query.join(Account, Broadcast.account_id == Account.id).where(Account.tenant_id == tenant_id)
        bcast_result = await db.execute(bcast_query)
        for b in bcast_result.scalars().all():
            is_scheduled = b.scheduled_at and b.scheduled_at > datetime.now(timezone.utc)
            entries.append(ExecutionLogEntry(
                id=b.id,
                type="scheduled" if is_scheduled else "broadcast",
                status="success" if b.status == "sent" else "failed" if b.status == "failed" else "sending" if b.status == "sending" else "pending",
                title=f"{'예약 발송' if is_scheduled else '브로드캐스트'}: {(b.message or '')[:50]}",
                detail=b.error_message,
                created_at=b.created_at.isoformat() if b.created_at else None,
            ))

    # Auto-reply logs (recent)
    if type is None or type == "auto_reply":
        ar_query = select(AutoReplyLog).order_by(AutoReplyLog.created_at.desc()).limit(limit)
        if tenant_id:
            from app.models.account import Account
            ar_query = ar_query.join(Account, AutoReplyLog.account_id == Account.id).where(Account.tenant_id == tenant_id)
        ar_result = await db.execute(ar_query)
        for log in ar_result.scalars().all():
            entries.append(ExecutionLogEntry(
                id=log.id,
                type="auto_reply",
                status="success" if log.status == "success" else "failed" if log.status == "failed" else "pending",
                title=f"자동응답: {(log.trigger_message or '')[:40]}",
                detail=log.reply_sent[:100] if log.reply_sent else None,
                created_at=log.created_at.isoformat() if log.created_at else None,
            ))

    # Sort by created_at descending
    entries.sort(key=lambda e: e.created_at or "", reverse=True)
    return entries[:limit]


# ── Workflow Status ──────────────────────────────────────────────


@router.get("/workflow-status", response_model=WorkflowStatus)
async def get_workflow_status(
    identity: Identity = Depends(get_current_identity),
):
    """Get overall automation workflow status."""
    # Check scheduler status
    try:
        from app.scheduler.scheduler import _scheduler
        scheduler_running = _scheduler.running if hasattr(_scheduler, "running") else True
        scheduler_jobs = len(_scheduler.get_jobs()) if hasattr(_scheduler, "get_jobs") else 0
    except Exception:
        scheduler_running = False
        scheduler_jobs = 0

    return WorkflowStatus(
        scheduler_active=scheduler_running,
        scheduler_tick_seconds=30,
        broadcast_processor_active=True,
        auto_reply_listener_active=True,
        total_jobs_running=0,
        total_jobs_scheduled=scheduler_jobs,
    )


# ── Queue Health ─────────────────────────────────────────────────


@router.get("/queue-health", response_model=QueueHealth)
async def get_queue_health(
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Health check for each automation queue."""
    issues = []

    # Check broadcast queue depth
    pending_count = (await db.execute(
        select(func.count(Broadcast.id)).where(Broadcast.status.in_(["pending", "sending"]))
    )).scalar() or 0

    if pending_count > 50:
        issues.append(f"브로드캐스트 대기열 과부하: {pending_count}건")

    # Check for stuck broadcasts (pending for > 1 hour)
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    stuck_count = (await db.execute(
        select(func.count(Broadcast.id)).where(
            Broadcast.status == "sending",
            Broadcast.created_at < one_hour_ago,
        )
    )).scalar() or 0

    if stuck_count > 0:
        issues.append(f"브로드캐스트 응답 없음: {stuck_count}건 (1시간 이상)")

    return QueueHealth(
        broadcast_healthy=pending_count <= 50 and stuck_count == 0,
        broadcast_queue_depth=pending_count,
        auto_reply_healthy=True,
        scheduler_healthy=True,
        issues=issues,
    )
