"""API router for Campaign CRUD — tenant-scoped."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_tenant_access
from app.core.logging import get_logger
from app.crud.campaign import get_campaign as campaign_crud_get_campaign
from app.crud.campaign import create_campaign as campaign_crud_create_campaign
from app.crud.campaign import update_campaign as campaign_crud_update_campaign
from app.crud.campaign import delete_campaign as campaign_crud_delete_campaign
from app.crud.campaign import list_campaigns as campaign_crud_list_campaigns
from app.crud.campaign import update_campaign_stats as campaign_crud_update_campaign_stats
from app.database import get_db
from app.schemas.campaign import (
    CampaignCreate,
    CampaignList,
    CampaignRead,
    CampaignUpdate,
)

router = APIRouter(prefix="/api/tenants/{tenant_id}/campaigns", tags=["campaigns"])
logger = get_logger(__name__)


@router.get("", response_model=CampaignList)
async def list_campaigns(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    status: str | None = Query(None),
    search: str | None = Query(None, max_length=200),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    await require_tenant_access(tenant_id, identity)
    items, total = await campaign_crud_list_campaigns(
        db, tenant_id, status=status, search=search, skip=skip, limit=limit,
    )
    return CampaignList(
        items=[CampaignRead.model_validate(c) for c in items],
        total=total,
    )


@router.get("/{campaign_id}", response_model=CampaignRead)
async def get_campaign(
    tenant_id: str,
    campaign_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_tenant_access(tenant_id, identity)
    campaign = await campaign_crud_get_campaign(db, campaign_id)
    if campaign is None or campaign.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="캠페인을 찾을 수 없습니다.")
    return campaign


@router.post("", response_model=CampaignRead, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    tenant_id: str,
    payload: CampaignCreate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_tenant_access(tenant_id, identity)
    campaign = await campaign_crud_create_campaign(db, tenant_id, payload)
    logger.info("campaign_created", tenant_id=tenant_id, campaign_id=campaign.id, name=campaign.name)
    return campaign


@router.put("/{campaign_id}", response_model=CampaignRead)
async def update_campaign(
    tenant_id: str,
    campaign_id: str,
    payload: CampaignUpdate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_tenant_access(tenant_id, identity)
    campaign = await campaign_crud_get_campaign(db, campaign_id)
    if campaign is None or campaign.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="캠페인을 찾을 수 없습니다.")
    updated = await campaign_crud_update_campaign(db, campaign, payload)
    logger.info("campaign_updated", tenant_id=tenant_id, campaign_id=campaign_id, status=updated.status)
    return updated


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(
    tenant_id: str,
    campaign_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_tenant_access(tenant_id, identity)
    campaign = await campaign_crud_get_campaign(db, campaign_id)
    if campaign is None or campaign.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="캠페인을 찾을 수 없습니다.")
    await campaign_crud_delete_campaign(db, campaign)
    logger.info("campaign_deleted", tenant_id=tenant_id, campaign_id=campaign_id)


@router.post("/{campaign_id}/recalc", response_model=CampaignRead)
async def recalc_campaign_stats(
    tenant_id: str,
    campaign_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Recalculate aggregated stats from linked broadcasts."""
    await require_tenant_access(tenant_id, identity)
    campaign = await campaign_crud_update_campaign_stats(db, campaign_id)
    if campaign is None or campaign.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="캠페인을 찾을 수 없습니다.")
    return campaign