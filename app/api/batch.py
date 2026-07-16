"""Bulk operations for broadcasts."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity
from app.core.logging import get_logger
from app.crud import broadcast as broadcast_crud
from app.database import get_db
from app.crud import account as account_crud

router = APIRouter(tags=["batch"])
logger = get_logger(__name__)


@router.post("/api/broadcast/batch-cancel")
async def batch_cancel_broadcasts(
    broadcast_ids: Annotated[str, Form(description="Comma-separated broadcast IDs")],
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    ids = [i.strip() for i in broadcast_ids.split(",") if i.strip()]
    if not ids:
        raise HTTPException(status_code=422, detail="No broadcast IDs provided")

    cancelled = 0
    errors = []
    for bid in ids:
        broadcast = await broadcast_crud.get_broadcast(db, bid)
        if broadcast is None:
            errors.append({"id": bid, "error": "not_found"})
            continue
        if identity.kind != "admin":
            account = await account_crud.get_account(db, broadcast.account_id)
            if account is None or (identity.tenant_id and account.tenant_id != identity.tenant_id):
                errors.append({"id": bid, "error": "access_denied"})
                continue
        if broadcast.recurring_interval_minutes is None:
            if broadcast.status in ("pending", "sending", "failed"):
                broadcast.status = "cancelled"
                cancelled += 1
            else:
                errors.append({"id": bid, "error": f"cannot cancel status={broadcast.status}"})
        else:
            updated = await broadcast_crud.cancel_recurring_broadcast(db, bid)
            if updated:
                cancelled += 1
            else:
                errors.append({"id": bid, "error": "cancel_failed"})

    await db.commit()
    logger.info("batch_cancel", total=len(ids), cancelled=cancelled, errors=len(errors))
    return {"cancelled": cancelled, "errors": errors, "total": len(ids)}


@router.post("/api/broadcast/batch-retry")
async def batch_retry_broadcasts(
    broadcast_ids: Annotated[str, Form(description="Comma-separated broadcast IDs")],
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    ids = [i.strip() for i in broadcast_ids.split(",") if i.strip()]
    if not ids:
        raise HTTPException(status_code=422, detail="No broadcast IDs provided")

    retried = 0
    errors = []
    for bid in ids:
        broadcast = await broadcast_crud.get_broadcast(db, bid)
        if broadcast is None:
            errors.append({"id": bid, "error": "not_found"})
            continue
        if broadcast.recurring_interval_minutes is not None:
            errors.append({"id": bid, "error": "is_recurring"})
            continue
        if broadcast.status != "failed":
            errors.append({"id": bid, "error": f"status={broadcast.status}"})
            continue
        if identity.kind != "admin":
            account = await account_crud.get_account(db, broadcast.account_id)
            if account is None or (identity.tenant_id and account.tenant_id != identity.tenant_id):
                errors.append({"id": bid, "error": "access_denied"})
                continue
        updated = await broadcast_crud.retry_broadcast(db, bid)
        if updated:
            retried += 1
        else:
            errors.append({"id": bid, "error": "retry_failed"})

    await db.commit()
    logger.info("batch_retry", total=len(ids), retried=retried, errors=len(errors))
    return {"retried": retried, "errors": errors, "total": len(ids)}
