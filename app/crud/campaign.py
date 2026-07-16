"""CRUD operations for Campaign."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaign import Campaign
from app.schemas.campaign import CampaignCreate, CampaignUpdate


async def list_campaigns(
    db: AsyncSession,
    tenant_id: str,
    status: str | None = None,
    search: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[Campaign], int]:
    conditions = [Campaign.tenant_id == tenant_id]
    if status:
        conditions.append(Campaign.status == status)
    if search:
        conditions.append(Campaign.name.ilike(f"%{search}%"))

    count_q = select(func.count()).select_from(Campaign).where(*conditions)
    total = await db.scalar(count_q) or 0

    q = (
        select(Campaign)
        .where(*conditions)
        .order_by(Campaign.updated_at.desc())
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(q)
    items = list(result.scalars().all())
    return items, total


async def get_campaign(db: AsyncSession, campaign_id: str) -> Campaign | None:
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    return result.scalar_one_or_none()


async def create_campaign(db: AsyncSession, tenant_id: str, payload: CampaignCreate) -> Campaign:
    campaign = Campaign(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        name=payload.name,
        description=payload.description,
        goal=payload.goal,
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return campaign


async def update_campaign(db: AsyncSession, campaign: Campaign, payload: CampaignUpdate) -> Campaign:
    update_data = payload.model_dump(exclude_unset=True)

    if "status" in update_data:
        if update_data["status"] == "active" and campaign.status != "active":
            update_data["started_at"] = datetime.now(timezone.utc)
        if update_data["status"] in ("completed", "cancelled") and campaign.status not in ("completed", "cancelled"):
            update_data["completed_at"] = datetime.now(timezone.utc)

    for field, value in update_data.items():
        setattr(campaign, field, value)

    await db.commit()
    await db.refresh(campaign)
    return campaign


async def delete_campaign(db: AsyncSession, campaign: Campaign) -> None:
    await db.delete(campaign)
    await db.commit()


async def update_campaign_stats(db: AsyncSession, campaign_id: str) -> Campaign | None:
    from sqlalchemy import func as sa_func
    from app.models.broadcast import Broadcast

    campaign = await get_campaign(db, campaign_id)
    if not campaign:
        return None

    count_q = select(sa_func.count()).select_from(Broadcast).where(
        Broadcast.campaign_id == campaign_id
    )
    total = await db.scalar(count_q) or 0

    sent_q = select(sa_func.count()).select_from(Broadcast).where(
        Broadcast.campaign_id == campaign_id,
        Broadcast.status == "sent",
    )
    failed_q = select(sa_func.count()).select_from(Broadcast).where(
        Broadcast.campaign_id == campaign_id,
        Broadcast.status == "failed",
    )
    sent = await db.scalar(sent_q) or 0
    failed = await db.scalar(failed_q) or 0

    campaign.total_broadcasts = total
    campaign.total_sent = sent
    campaign.total_failed = failed
    await db.commit()
    await db.refresh(campaign)
    return campaign
