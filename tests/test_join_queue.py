"""Tests for the Smart Join Queue — model CRUD, service logic, and API endpoints.

Uses the same test patterns as test_link_inspector.py (async DB, mock Telethon).
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import join_queue as queue_crud
from app.models.join_queue import JoinQueueConfig, JoinQueueItem


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_account_id() -> str:
    return "test-account-001"


@pytest.fixture
def sample_links() -> list[dict]:
    return [
        {"raw_link": "https://t.me/testgroup1", "title": "Test Group 1", "chat_type": "group"},
        {"raw_link": "https://t.me/+abc123", "title": "Test Channel", "chat_type": "channel"},
        {"raw_link": "@testgroup3", "title": "Test Group 3", "chat_type": "megagroup"},
    ]


# ── CRUD Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_to_queue(db_session: AsyncSession, sample_account_id):
    """Adding a single item should return a queued item with position 1."""
    item = await queue_crud.add_to_queue(
        db_session,
        account_id=sample_account_id,
        raw_link="https://t.me/test",
        title="Test",
        chat_type="group",
    )
    assert item.account_id == sample_account_id
    assert item.raw_link == "https://t.me/test"
    assert item.status == "queued"
    assert item.position == 1
    assert item.id is not None


@pytest.mark.asyncio
async def test_add_many_to_queue(db_session: AsyncSession, sample_account_id, sample_links):
    """Adding multiple items should assign sequential positions."""
    items = await queue_crud.add_many_to_queue(db_session, sample_account_id, sample_links)
    assert len(items) == 3
    assert items[0].position == 1
    assert items[1].position == 2
    assert items[2].position == 3
    assert items[0].status == "queued"


@pytest.mark.asyncio
async def test_list_queue(db_session: AsyncSession, sample_account_id, sample_links):
    """Listing queue should return items ordered by position."""
    await queue_crud.add_many_to_queue(db_session, sample_account_id, sample_links)
    items, total = await queue_crud.list_queue(db_session, sample_account_id)
    assert total == 3
    assert len(items) == 3
    assert items[0].position == 1
    assert items[2].position == 3


@pytest.mark.asyncio
async def test_list_queue_with_status_filter(db_session: AsyncSession, sample_account_id, sample_links):
    """Listing queue with status filter should only return matching items."""
    await queue_crud.add_many_to_queue(db_session, sample_account_id, sample_links)
    items, total = await queue_crud.list_queue(db_session, sample_account_id, status_filter="success")
    assert total == 0
    assert len(items) == 0

    # Mark one as success
    all_items, _ = await queue_crud.list_queue(db_session, sample_account_id)
    await queue_crud.update_queue_item_status(db_session, all_items[0], "success")
    items, total = await queue_crud.list_queue(db_session, sample_account_id, status_filter="success")
    assert total == 1


@pytest.mark.asyncio
async def test_claim_next_queued(db_session: AsyncSession, sample_account_id, sample_links):
    """Claiming should atomically get the next queued item and mark it processing."""
    await queue_crud.add_many_to_queue(db_session, sample_account_id, sample_links)
    claimed = await queue_crud.claim_next_queued(db_session, sample_account_id)
    assert claimed is not None
    assert claimed.status == "processing"
    assert claimed.position == 1

    # Second claim should get position 2
    claimed2 = await queue_crud.claim_next_queued(db_session, sample_account_id)
    assert claimed2 is not None
    assert claimed2.status == "processing"
    assert claimed2.position == 2


@pytest.mark.asyncio
async def test_claim_next_queued_empty(db_session: AsyncSession, sample_account_id):
    """Claiming from an empty queue should return None."""
    claimed = await queue_crud.claim_next_queued(db_session, sample_account_id)
    assert claimed is None


@pytest.mark.asyncio
async def test_update_queue_item_status_success(db_session: AsyncSession, sample_account_id):
    """Updating status to success should set processed_at."""
    item = await queue_crud.add_to_queue(db_session, sample_account_id, raw_link="https://t.me/test")
    assert item.processed_at is None
    await queue_crud.update_queue_item_status(db_session, item, "success", chat_id="12345")
    assert item.status == "success"
    assert item.chat_id == "12345"
    assert item.processed_at is not None


@pytest.mark.asyncio
async def test_update_queue_item_status_flood_wait(db_session: AsyncSession, sample_account_id):
    """Updating status to flood_wait should set flood_wait_until."""
    item = await queue_crud.add_to_queue(db_session, sample_account_id, raw_link="https://t.me/test")
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    await queue_crud.update_queue_item_status(db_session, item, "flood_wait",
                                               error_message="Flood wait 60s",
                                               flood_wait_until=future)
    assert item.status == "flood_wait"
    assert item.flood_wait_until is not None
    assert item.error_message == "Flood wait 60s"


@pytest.mark.asyncio
async def test_remove_from_queue(db_session: AsyncSession, sample_account_id):
    """Removing an item should delete it from the DB."""
    item = await queue_crud.add_to_queue(db_session, sample_account_id, raw_link="https://t.me/test")
    item_id = str(item.id)
    removed = await queue_crud.remove_from_queue(db_session, item_id)
    assert removed is True
    # Verify it's gone
    fetched = await queue_crud.get_queue_item(db_session, item_id)
    assert fetched is None


@pytest.mark.asyncio
async def test_clear_queue(db_session: AsyncSession, sample_account_id, sample_links):
    """Clearing the queue should remove all items."""
    await queue_crud.add_many_to_queue(db_session, sample_account_id, sample_links)
    cleared = await queue_crud.clear_queue(db_session, sample_account_id)
    assert cleared == 3
    items, total = await queue_crud.list_queue(db_session, sample_account_id)
    assert total == 0


# ── Config Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_or_create_config(db_session: AsyncSession, sample_account_id):
    """Getting config for a new account should create defaults."""
    config = await queue_crud.get_or_create_config(db_session, sample_account_id)
    assert config.account_id == sample_account_id
    assert config.is_paused is False
    assert config.joins_per_hour == 5
    assert config.max_daily_joins == 20


@pytest.mark.asyncio
async def test_update_config(db_session: AsyncSession, sample_account_id):
    """Updating config should persist changes."""
    config = await queue_crud.update_config(db_session, sample_account_id,
                                              is_paused=True,
                                              joins_per_hour=3,
                                              max_daily_joins=10)
    assert config.is_paused is True
    assert config.joins_per_hour == 3
    assert config.max_daily_joins == 10

    # Re-fetch and verify persistence
    config2 = await queue_crud.get_or_create_config(db_session, sample_account_id)
    assert config2.is_paused is True
    assert config2.joins_per_hour == 3


@pytest.mark.asyncio
async def test_update_config_partial(db_session: AsyncSession, sample_account_id):
    """Partial update should only change specified fields."""
    config = await queue_crud.update_config(db_session, sample_account_id, is_paused=True)
    assert config.is_paused is True
    assert config.joins_per_hour == 5  # unchanged default
    assert config.max_daily_joins == 20  # unchanged default


# ── Service Logic Tests (mocked Telethon) ────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_telegram_link():
    """Verify link parser handles various formats correctly."""
    from app.services.link_inspector_service import parse_telegram_link

    # Username
    kind, value = parse_telegram_link("@testgroup")
    assert kind == "username"
    assert value == "testgroup"

    kind, value = parse_telegram_link("https://t.me/testgroup")
    assert kind == "username"
    assert value == "testgroup"

    # Invite link
    kind, value = parse_telegram_link("https://t.me/+abc123")
    assert kind == "invite"
    assert value == "abc123"

    kind, value = parse_telegram_link("https://t.me/joinchat/abc123")
    assert kind == "invite"
    assert value == "abc123"

    # Invalid
    kind, value = parse_telegram_link("not a link")
    assert kind == "invalid"


# ── conftest support ─────────────────────────────────────────────────────────


@pytest.fixture
def anyio_backend():
    return "asyncio"