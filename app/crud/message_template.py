"""CRUD operations for MessageTemplate."""

import json
import uuid

from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message_template import MessageTemplate
from app.schemas.message_template import MessageTemplateCreate, MessageTemplateUpdate


async def list_templates(
    db: AsyncSession,
    tenant_id: str,
    category: str | None = None,
    search: str | None = None,
    favorite_only: bool = False,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[MessageTemplate], int]:
    """List templates for a tenant with optional filters."""
    conditions = [MessageTemplate.tenant_id == tenant_id]

    if category:
        conditions.append(MessageTemplate.category == category)
    if favorite_only:
        conditions.append(MessageTemplate.is_favorite == True)
    if search:
        conditions.append(
            or_(
                MessageTemplate.name.ilike(f"%{search}%"),
                MessageTemplate.content.ilike(f"%{search}%"),
            )
        )

    # Count total
    count_q = select(func.count()).select_from(MessageTemplate).where(*conditions)
    total = await db.scalar(count_q) or 0

    # Fetch paginated
    q = (
        select(MessageTemplate)
        .where(*conditions)
        .order_by(MessageTemplate.is_favorite.desc(), MessageTemplate.updated_at.desc())
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(q)
    items = list(result.scalars().all())

    return items, total


async def get_template(db: AsyncSession, template_id: str) -> MessageTemplate | None:
    """Get a single template by ID."""
    result = await db.execute(
        select(MessageTemplate).where(MessageTemplate.id == template_id)
    )
    return result.scalar_one_or_none()


async def create_template(
    db: AsyncSession, tenant_id: str, payload: MessageTemplateCreate
) -> MessageTemplate:
    """Create a new template."""
    template = MessageTemplate(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        name=payload.name,
        category=payload.category,
        content=payload.content,
        variables=json.dumps(payload.variables, ensure_ascii=False),
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return template


async def update_template(
    db: AsyncSession, template: MessageTemplate, payload: MessageTemplateUpdate
) -> MessageTemplate:
    """Update an existing template."""
    update_data = payload.model_dump(exclude_unset=True)
    if "variables" in update_data and update_data["variables"] is not None:
        update_data["variables"] = json.dumps(update_data["variables"], ensure_ascii=False)

    for field, value in update_data.items():
        setattr(template, field, value)

    await db.commit()
    await db.refresh(template)
    return template


async def delete_template(db: AsyncSession, template: MessageTemplate) -> None:
    """Delete a template."""
    await db.delete(template)
    await db.commit()


async def increment_use_count(db: AsyncSession, template: MessageTemplate) -> MessageTemplate:
    """Increment a template's use_count."""
    template.use_count += 1
    await db.commit()
    await db.refresh(template)
    return template