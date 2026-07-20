"""Integration tests for AI Content Studio scheduler on/off behavior."""

import pytest
from sqlalchemy import select

from app.api.content_studio import run_daily_content_generation
from app.crud.content_calendar import create_content_calendar_setting
from app.models.broadcast import Broadcast
from app.models.content_calendar import ContentCalendarSetting


@pytest.mark.asyncio
async def test_disabled_setting_skips_generation(client, db_session):
    setting = ContentCalendarSetting(
        account_id="acc-1",
        tenant_id="tenant-1",
        enabled=False,
        daily_count=3,
        content_types=["promotional", "announcement"],
        tone="short",
        group_ids=["-1001"],
        timezone="Asia/Seoul",
        send_hour=10,
    )
    db_session.add(setting)
    await db_session.commit()
    await db_session.refresh(setting)

    await run_daily_content_generation(db_session)
    await db_session.refresh(setting)

    assert setting.last_generated_at is None
    assert setting.next_generate_at is None


@pytest.mark.asyncio
async def test_enabled_setting_generates_broadcasts(client, db_session):
    setting = ContentCalendarSetting(
        account_id="acc-1",
        tenant_id="tenant-1",
        enabled=True,
        daily_count=2,
        content_types=["promotional", "announcement"],
        tone="short",
        group_ids=["-1001"],
        timezone="Asia/Seoul",
        send_hour=10,
    )
    db_session.add(setting)
    await db_session.commit()
    await db_session.refresh(setting)

    # Verify broadcast count before
    before_result = await db_session.execute(
        select(Broadcast).where(Broadcast.account_id == "acc-1")
    )
    before = list(before_result.scalars().all())
    before_count = len(before)

    await run_daily_content_generation(db_session)
    await db_session.refresh(setting)

    after_result = await db_session.execute(
        select(Broadcast).where(Broadcast.account_id == "acc-1")
    )
    after = list(after_result.scalars().all())
    after_count = len(after)

    assert after_count >= before_count + setting.daily_count
    assert setting.last_generated_at is not None
    assert setting.next_generate_at is not None
