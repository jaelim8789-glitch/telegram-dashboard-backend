"""API router for Message Template CRUD — tenant-scoped."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_tenant_access
from app.core.logging import get_logger
from app.crud import message_template as template_crud
from app.database import get_db
from app.schemas.message_template import (
    CategoryType,
    MessageTemplateCreate,
    MessageTemplateList,
    MessageTemplateRead,
    MessageTemplateUpdate,
)

router = APIRouter(prefix="/api/tenants/{tenant_id}/templates", tags=["message-templates"])
logger = get_logger(__name__)


@router.get("", response_model=MessageTemplateList)
async def list_templates(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    category: CategoryType | None = Query(None),
    search: str | None = Query(None, max_length=200),
    favorite_only: bool = Query(False),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """List all message templates for a tenant with optional filters."""
    await require_tenant_access(tenant_id, identity)
    items, total = await template_crud.list_templates(
        db, tenant_id, category=category, search=search,
        favorite_only=favorite_only, skip=skip, limit=limit,
    )
    return MessageTemplateList(
        items=[MessageTemplateRead.model_validate(t) for t in items],
        total=total,
    )


@router.get("/{template_id}", response_model=MessageTemplateRead)
async def get_template(
    tenant_id: str,
    template_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get a single message template."""
    await require_tenant_access(tenant_id, identity)
    template = await template_crud.get_template(db, template_id)
    if template is None or template.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="템플릿을 찾을 수 없습니다.")
    return template


@router.post("", response_model=MessageTemplateRead, status_code=status.HTTP_201_CREATED)
async def create_template(
    tenant_id: str,
    payload: MessageTemplateCreate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Create a new message template."""
    await require_tenant_access(tenant_id, identity)
    template = await template_crud.create_template(db, tenant_id, payload)
    logger.info("template_created", tenant_id=tenant_id, template_id=template.id, name=template.name)
    return template


@router.put("/{template_id}", response_model=MessageTemplateRead)
async def update_template(
    tenant_id: str,
    template_id: str,
    payload: MessageTemplateUpdate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Update an existing message template."""
    await require_tenant_access(tenant_id, identity)
    template = await template_crud.get_template(db, template_id)
    if template is None or template.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="템플릿을 찾을 수 없습니다.")
    updated = await template_crud.update_template(db, template, payload)
    logger.info("template_updated", tenant_id=tenant_id, template_id=template_id)
    return updated


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    tenant_id: str,
    template_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Delete a message template."""
    await require_tenant_access(tenant_id, identity)
    template = await template_crud.get_template(db, template_id)
    if template is None or template.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="템플릿을 찾을 수 없습니다.")
    await template_crud.delete_template(db, template)
    logger.info("template_deleted", tenant_id=tenant_id, template_id=template_id)


@router.post("/{template_id}/use", response_model=MessageTemplateRead)
async def use_template(
    tenant_id: str,
    template_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Increment a template's use_count (called when a template is used in a broadcast)."""
    await require_tenant_access(tenant_id, identity)
    template = await template_crud.get_template(db, template_id)
    if template is None or template.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="템플릿을 찾을 수 없습니다.")
    result = await template_crud.increment_use_count(db, template)
    logger.info("template_used", tenant_id=tenant_id, template_id=template_id, use_count=result.use_count)
    return result