from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import account as account_crud
from app.models.content_calendar import ContentCalendarSetting
from app.schemas.content_studio import ContentCalendarSettingCreate, ContentCalendarSettingUpdate


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def create_content_calendar_setting(
    db: AsyncSession, data: ContentCalendarSettingCreate, tenant_id: str
) -> ContentCalendarSetting:
    account = await account_crud.get_account(db, data.account_id)
    if account is None:
        raise ValueError(f"Account {data.account_id} not found")

    setting = ContentCalendarSetting(
        account_id=data.account_id,
        tenant_id=tenant_id,
        enabled=data.enabled,
        daily_count=data.daily_count,
        content_types=data.content_types,
        tone=data.tone,
        group_ids=data.group_ids,
        timezone=data.timezone,
        send_hour=data.send_hour,
    )
    db.add(setting)
    await db.commit()
    await db.refresh(setting)
    return setting


async def get_content_calendar_setting(db: AsyncSession, setting_id: str) -> ContentCalendarSetting | None:
    return await db.get(ContentCalendarSetting, setting_id)


async def list_content_calendar_settings(db: AsyncSession, account_id: str | None = None) -> list[ContentCalendarSetting]:
    query = select(ContentCalendarSetting).order_by(ContentCalendarSetting.created_at.desc())
    if account_id:
        query = query.where(ContentCalendarSetting.account_id == account_id)
    result = await db.execute(query)
    return list(result.scalars().all())


async def update_content_calendar_setting(
    db: AsyncSession, setting_id: str, data: ContentCalendarSettingUpdate
) -> ContentCalendarSetting | None:
    setting = await get_content_calendar_setting(db, setting_id)
    if setting is None:
        return None

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(setting, field, value)

    await db.commit()
    await db.refresh(setting)
    return setting


async def delete_content_calendar_setting(db: AsyncSession, setting_id: str) -> bool:
    setting = await get_content_calendar_setting(db, setting_id)
    if setting is None:
        return False
    await db.delete(setting)
    await db.commit()
    return True
