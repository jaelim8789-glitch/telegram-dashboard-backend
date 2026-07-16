from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_admin
from app.crud import broadcast as broadcast_crud
from app.database import get_db
from app.schemas.broadcast import BroadcastRead

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.get("/upcoming", response_model=list[BroadcastRead])
async def read_upcoming(
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """List upcoming scheduled broadcasts, tenant-isolated."""
    return await broadcast_crud.list_upcoming_scheduled_broadcasts(db, identity=identity)


@router.get("/status")
async def get_scheduler_status(
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get current scheduler status: next tick, queue depth, active jobs.

    Shows aggregate counts across all tenants (admin) or scoped to the
    caller's tenant (non-admin users).
    """
    try:
        from app.scheduler.scheduler import (
            DISPATCH_INTERVAL_SECONDS,
            _running_broadcasts,
            _running_recurring,
            _running_macros,
            scheduler,
        )
    except ImportError:
        return {
            "status": "unavailable",
            "detail": "스케줄러 모듈을 찾을 수 없습니다.",
        }

    now = broadcast_crud.utcnow_naive()
    due = await broadcast_crud.list_due_scheduled_broadcasts(db)

    # Filter by tenant if not admin
    if identity.kind != "admin" and identity.tenant_id:
        from app.models.account import Account
        from sqlalchemy import select

        account_ids = select(Account.id).where(Account.tenant_id == identity.tenant_id)
        due = [b for b in due if b.account_id in account_ids]

    next_run_time = scheduler.get_job("dispatch_due_broadcasts")
    next_run = str(next_run_time.next_run_time) if next_run_time and next_run_time.next_run_time else None

    return {
        "tick_interval_seconds": DISPATCH_INTERVAL_SECONDS,
        "next_tick_at": next_run,
        "due_broadcasts_count": len(due),
        "running_broadcasts_count": len(_running_broadcasts),
        "running_recurring_count": len(_running_recurring),
        "running_reply_macros_count": len(_running_macros),
        "scheduler_running": scheduler.running,
    }


@router.post("/pause-job", dependencies=[Depends(require_admin)])
async def pause_scheduler_job(job_id: str):
    """Pause a scheduler job by ID (admin only).

    Jobs: dispatch_due_broadcasts, dispatch_due_reply_macros,
    check_usdt_payments, downgrade_expired_tenants, process_join_queue.
    """
    try:
        from app.scheduler.scheduler import scheduler
    except ImportError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="스케줄러 모듈을 찾을 수 없습니다.")

    job = scheduler.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"작업 '{job_id}'을(를) 찾을 수 없습니다.")

    scheduler.pause_job(job_id)
    return {"status": "paused", "job_id": job_id}


@router.post("/resume-job", dependencies=[Depends(require_admin)])
async def resume_scheduler_job(job_id: str):
    """Resume a paused scheduler job by ID (admin only)."""
    try:
        from app.scheduler.scheduler import scheduler
    except ImportError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="스케줄러 모듈을 찾을 수 없습니다.")

    job = scheduler.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"작업 '{job_id}'을(를) 찾을 수 없습니다.")

    scheduler.resume_job(job_id)
    return {"status": "resumed", "job_id": job_id}