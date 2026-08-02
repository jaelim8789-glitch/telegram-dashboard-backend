"""Escrow API — create, fund, milestone management, dispute, release."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.api.deps import get_current_identity, Identity
from app.models.escrow import Escrow, EscrowMilestone, EscrowMessage

router = APIRouter(tags=["escrow"])


# ── Schemas ──────────────────────────────────────────────────────────


class EscrowCreateRequest(BaseModel):
    chat_id: str
    buyer_id: str
    seller_id: str
    title: str = Field(..., max_length=200)
    description: str | None = None
    amount: float = Field(..., gt=0)
    currency: str = "KRW"


class MilestoneCreateRequest(BaseModel):
    title: str = Field(..., max_length=200)
    description: str | None = None
    amount: float = Field(..., gt=0)
    order_index: int = 0


class MilestoneActionRequest(BaseModel):
    pass


class EscrowMessageRequest(BaseModel):
    content: str = Field(..., max_length=5000)
    message_type: str = "text"


class EscrowResolveRequest(BaseModel):
    resolution: str = Field(..., max_length=2000)


# ── Escrow CRUD ──────────────────────────────────────────────────────


@router.post("/api/escrow")
async def create_escrow(
    payload: EscrowCreateRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    escrow = Escrow(
        tenant_id=identity.tenant_id,
        account_id="",
        chat_id=payload.chat_id,
        buyer_id=payload.buyer_id,
        seller_id=payload.seller_id,
        title=payload.title,
        description=payload.description,
        amount=payload.amount,
        currency=payload.currency,
        status="pending",
    )
    db.add(escrow)
    await db.commit()
    await db.refresh(escrow)
    return _escrow_to_dict(escrow)


@router.get("/api/escrow")
async def list_escrows(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    query = select(Escrow).where(Escrow.tenant_id == identity.tenant_id)
    if status:
        query = query.where(Escrow.status == status)
    query = query.order_by(Escrow.created_at.desc()).limit(50)
    result = await db.execute(query)
    return [_escrow_to_dict(e) for e in result.scalars().all()]


@router.get("/api/escrow/{escrow_id}")
async def get_escrow(
    escrow_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    result = await db.execute(
        select(Escrow).where(Escrow.id == escrow_id, Escrow.tenant_id == identity.tenant_id)
    )
    escrow = result.scalar_one_or_none()
    if not escrow:
        raise HTTPException(status_code=404, detail="에스크로를 찾을 수 없습니다.")
    data = _escrow_to_dict(escrow)

    ms_result = await db.execute(
        select(EscrowMilestone)
        .where(EscrowMilestone.escrow_id == escrow_id)
        .order_by(EscrowMilestone.order_index)
    )
    data["milestones"] = [_milestone_to_dict(m) for m in ms_result.scalars().all()]

    msg_result = await db.execute(
        select(EscrowMessage)
        .where(EscrowMessage.escrow_id == escrow_id)
        .order_by(EscrowMessage.created_at)
        .limit(100)
    )
    data["messages"] = [_message_to_dict(m) for m in msg_result.scalars().all()]
    return data


@router.post("/api/escrow/{escrow_id}/fund")
async def fund_escrow(
    escrow_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    result = await db.execute(
        select(Escrow).where(Escrow.id == escrow_id, Escrow.tenant_id == identity.tenant_id)
    )
    escrow = result.scalar_one_or_none()
    if not escrow:
        raise HTTPException(status_code=404, detail="에스크로를 찾을 수 없습니다.")
    if escrow.status != "pending":
        raise HTTPException(status_code=400, detail=f"현재 상태({escrow.status})에서는 예치할 수 없습니다.")

    escrow.status = "funded"
    await db.commit()
    return _escrow_to_dict(escrow)


@router.post("/api/escrow/{escrow_id}/start")
async def start_escrow(
    escrow_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    result = await db.execute(
        select(Escrow).where(Escrow.id == escrow_id, Escrow.tenant_id == identity.tenant_id)
    )
    escrow = result.scalar_one_or_none()
    if not escrow:
        raise HTTPException(status_code=404, detail="에스크로를 찾을 수 없습니다.")
    if escrow.status != "funded":
        raise HTTPException(status_code=400, detail="예치 완료 후 작업을 시작할 수 있습니다.")

    escrow.status = "in_progress"
    await db.commit()
    return _escrow_to_dict(escrow)


@router.post("/api/escrow/{escrow_id}/complete")
async def complete_escrow(
    escrow_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    from datetime import datetime, timezone
    result = await db.execute(
        select(Escrow).where(Escrow.id == escrow_id, Escrow.tenant_id == identity.tenant_id)
    )
    escrow = result.scalar_one_or_none()
    if not escrow:
        raise HTTPException(status_code=404, detail="에스크로를 찾을 수 없습니다.")

    escrow.status = "completed"
    escrow.released_at = datetime.now(timezone.utc)
    await db.commit()
    return _escrow_to_dict(escrow)


@router.post("/api/escrow/{escrow_id}/dispute")
async def dispute_escrow(
    escrow_id: str,
    payload: EscrowResolveRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    from datetime import datetime, timezone
    result = await db.execute(
        select(Escrow).where(Escrow.id == escrow_id, Escrow.tenant_id == identity.tenant_id)
    )
    escrow = result.scalar_one_or_none()
    if not escrow:
        raise HTTPException(status_code=404, detail="에스크로를 찾을 수 없습니다.")

    escrow.status = "disputed"
    escrow.disputed_at = datetime.now(timezone.utc)
    escrow.dispute_reason = payload.resolution
    await db.commit()
    return _escrow_to_dict(escrow)


@router.post("/api/escrow/{escrow_id}/resolve")
async def resolve_escrow(
    escrow_id: str,
    payload: EscrowResolveRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    from datetime import datetime, timezone
    result = await db.execute(
        select(Escrow).where(Escrow.id == escrow_id, Escrow.tenant_id == identity.tenant_id)
    )
    escrow = result.scalar_one_or_none()
    if not escrow:
        raise HTTPException(status_code=404, detail="에스크로를 찾을 수 없습니다.")

    escrow.status = "resolved"
    escrow.resolved_at = datetime.now(timezone.utc)
    escrow.resolution = payload.resolution
    await db.commit()
    return _escrow_to_dict(escrow)


# ── Milestones ───────────────────────────────────────────────────────


@router.post("/api/escrow/{escrow_id}/milestones")
async def create_milestone(
    escrow_id: str,
    payload: MilestoneCreateRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    result = await db.execute(
        select(Escrow).where(Escrow.id == escrow_id, Escrow.tenant_id == identity.tenant_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="에스크로를 찾을 수 없습니다.")

    milestone = EscrowMilestone(
        escrow_id=escrow_id,
        title=payload.title,
        description=payload.description,
        amount=payload.amount,
        order_index=payload.order_index,
    )
    db.add(milestone)
    await db.commit()
    await db.refresh(milestone)
    return _milestone_to_dict(milestone)


@router.post("/api/escrow/{escrow_id}/milestones/{milestone_id}/submit")
async def submit_milestone(
    escrow_id: str,
    milestone_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    from datetime import datetime, timezone
    result = await db.execute(
        select(EscrowMilestone).where(
            EscrowMilestone.id == milestone_id,
            EscrowMilestone.escrow_id == escrow_id,
        )
    )
    ms = result.scalar_one_or_none()
    if not ms:
        raise HTTPException(status_code=404, detail="마일스톤을 찾을 수 없습니다.")

    ms.status = "submitted"
    ms.submitted_at = datetime.now(timezone.utc)
    await db.commit()
    return _milestone_to_dict(ms)


@router.post("/api/escrow/{escrow_id}/milestones/{milestone_id}/approve")
async def approve_milestone(
    escrow_id: str,
    milestone_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    from datetime import datetime, timezone
    result = await db.execute(
        select(EscrowMilestone).where(
            EscrowMilestone.id == milestone_id,
            EscrowMilestone.escrow_id == escrow_id,
        )
    )
    ms = result.scalar_one_or_none()
    if not ms:
        raise HTTPException(status_code=404, detail="마일스톤을 찾을 수 없습니다.")

    ms.status = "approved"
    ms.reviewed_at = datetime.now(timezone.utc)
    await db.commit()
    return _milestone_to_dict(ms)


# ── Messages ─────────────────────────────────────────────────────────


@router.post("/api/escrow/{escrow_id}/messages")
async def send_escrow_message(
    escrow_id: str,
    payload: EscrowMessageRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    result = await db.execute(
        select(Escrow).where(Escrow.id == escrow_id, Escrow.tenant_id == identity.tenant_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="에스크로를 찾을 수 없습니다.")

    msg = EscrowMessage(
        escrow_id=escrow_id,
        sender_id=identity.tenant_id,
        content=payload.content,
        message_type=payload.message_type,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return _message_to_dict(msg)


@router.get("/api/escrow/{escrow_id}/messages")
async def get_escrow_messages(
    escrow_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    result = await db.execute(
        select(EscrowMessage)
        .where(EscrowMessage.escrow_id == escrow_id)
        .order_by(EscrowMessage.created_at)
        .limit(200)
    )
    return [_message_to_dict(m) for m in result.scalars().all()]


# ── Helpers ──────────────────────────────────────────────────────────


def _escrow_to_dict(e: Escrow) -> dict:
    return {
        "id": e.id,
        "chat_id": e.chat_id,
        "buyer_id": e.buyer_id,
        "seller_id": e.seller_id,
        "title": e.title,
        "description": e.description,
        "amount": e.amount,
        "currency": e.currency,
        "status": e.status,
        "payment_id": e.payment_id,
        "released_at": e.released_at.isoformat() if e.released_at else None,
        "disputed_at": e.disputed_at.isoformat() if e.disputed_at else None,
        "dispute_reason": e.dispute_reason,
        "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
        "resolution": e.resolution,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "updated_at": e.updated_at.isoformat() if e.updated_at else None,
    }


def _milestone_to_dict(m: EscrowMilestone) -> dict:
    return {
        "id": m.id,
        "escrow_id": m.escrow_id,
        "title": m.title,
        "description": m.description,
        "amount": m.amount,
        "order_index": m.order_index,
        "status": m.status,
        "submitted_at": m.submitted_at.isoformat() if m.submitted_at else None,
        "reviewed_at": m.reviewed_at.isoformat() if m.reviewed_at else None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _message_to_dict(m: EscrowMessage) -> dict:
    return {
        "id": m.id,
        "escrow_id": m.escrow_id,
        "sender_id": m.sender_id,
        "content": m.content,
        "message_type": m.message_type,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }
