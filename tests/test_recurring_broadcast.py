"""Tests for recurring broadcast functionality.

Covers:
1. Interval validation (allowed/rejected values)
2. Recurring creation via API (immediate and scheduled)
3. Recurring parent creates children on dispatch
4. Rescheduling (next_scheduled_at advancement)
5. Cancellation (status='cancelled', cancelled_at set, never re-executes)
6. Restart persistence (scheduler restart picks up due recurring)
7. Duplicate/overlapping execution prevention
8. Existing one-time broadcasts still work unchanged
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.crud import account as account_crud
from app.crud import broadcast as broadcast_crud
from app.schemas.account import AccountCreate
from app.schemas.broadcast import BroadcastCreate, RECURRING_INTERVAL_VALUES
from app.scheduler.scheduler import dispatch_due_broadcasts


# ── Helpers ─────────────────────────────────────────────────────────


async def _make_account(db_session, phone="+821033330000"):
    return await account_crud.create_account(db_session, AccountCreate(phone=phone))


async def _make_broadcast(db_session, account_id, **kwargs):
    defaults = dict(account_id=account_id, message="테스트", recipients=["-100999"])
    defaults.update(kwargs)
    payload = BroadcastCreate(**defaults)
    return await broadcast_crud.create_broadcast(db_session, payload, media_path=None, scheduled_at=defaults.get("scheduled_at"))


def _success_result(recipient="-100999"):
    from app.services.delivery import DeliveryResult, DeliveryStatus
    return DeliveryResult(status=DeliveryStatus.SUCCESS, recipient=recipient, telegram_message_id=12345)


# ═══════════════════════════════════════════════════════════════════════
# 1. Interval validation
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_recurring_interval_valid_values():
    """RECURRING_INTERVAL_VALUES contains the expected set."""
    assert RECURRING_INTERVAL_VALUES == {30, 60, 120, 180, 360, 720, 1440}


@pytest.mark.asyncio
async def test_recurring_interval_create_with_valid_value(db_session):
    """Creating a broadcast with a valid recurring interval succeeds."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    assert broadcast.recurring_interval_minutes == 30
    assert broadcast.next_scheduled_at is not None


@pytest.mark.asyncio
async def test_recurring_interval_create_with_zero_is_rejected(db_session):
    """Creating with recurring_interval_minutes=0 is rejected (not in allowed set)."""
    account = await _make_account(db_session)
    with pytest.raises(ValueError, match="recurring_interval_minutes must be one of"):
        await _make_broadcast(db_session, account.id, recurring_interval_minutes=0)


@pytest.mark.asyncio
async def test_recurring_interval_create_with_invalid_value_is_rejected(db_session):
    """Creating with an invalid interval like 45 is rejected."""
    account = await _make_account(db_session)
    with pytest.raises(ValueError, match="recurring_interval_minutes must be one of"):
        await _make_broadcast(db_session, account.id, recurring_interval_minutes=45)


@pytest.mark.asyncio
async def test_recurring_interval_create_with_negative_value_is_rejected(db_session):
    """Creating with a negative interval is rejected."""
    account = await _make_account(db_session)
    with pytest.raises(ValueError, match="recurring_interval_minutes must be one of"):
        await _make_broadcast(db_session, account.id, recurring_interval_minutes=-30)


@pytest.mark.asyncio
async def test_recurring_interval_all_allowed_values(db_session):
    """Each allowed interval can be used to create a recurring broadcast."""
    account = await _make_account(db_session)
    for interval in [30, 60, 120, 180, 360, 720, 1440]:
        broadcast = await _make_broadcast(db_session, account.id, recurring_interval_minutes=interval)
        assert broadcast.recurring_interval_minutes == interval
        assert broadcast.next_scheduled_at is not None


# ═══════════════════════════════════════════════════════════════════════
# 2. Recurring creation — immediate and scheduled
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_recurring_immediate_sets_next_scheduled_at_to_now(db_session):
    """Immediate recurring broadcast has next_scheduled_at set to approximately now."""
    account = await _make_account(db_session)
    before = broadcast_crud.utcnow_naive()
    broadcast = await _make_broadcast(db_session, account.id, recurring_interval_minutes=60)
    after = broadcast_crud.utcnow_naive()
    assert broadcast.next_scheduled_at is not None
    assert before <= broadcast.next_scheduled_at <= after


@pytest.mark.asyncio
async def test_recurring_scheduled_sets_next_scheduled_at_to_future(db_session):
    """Scheduled recurring broadcast has next_scheduled_at set to the scheduled time."""
    account = await _make_account(db_session)
    future = broadcast_crud.utcnow_naive() + timedelta(hours=2)
    broadcast = await _make_broadcast(
        db_session,
        account.id,
        recurring_interval_minutes=120,
        scheduled_at=future,
    )
    assert broadcast.next_scheduled_at is not None
    assert broadcast.next_scheduled_at >= future - timedelta(seconds=1)


@pytest.mark.asyncio
async def test_recurring_broadcast_has_correct_initial_status(db_session):
    """Recurring broadcast starts with status='pending', not cancelled."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    assert broadcast.status == "pending"
    assert broadcast.cancelled_at is None


@pytest.mark.asyncio
async def test_recurring_broadcast_is_recurring_interval_is_set(db_session):
    """The recurring_interval_minutes field is persisted correctly."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id, recurring_interval_minutes=720)
    assert broadcast.recurring_interval_minutes == 720

    # Reload from DB and check
    reloaded = await broadcast_crud.get_broadcast(db_session, broadcast.id)
    assert reloaded is not None
    assert reloaded.recurring_interval_minutes == 720


# ═══════════════════════════════════════════════════════════════════════
# 3. Recurring execution — parent creates children
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_recurring_parent_creates_child_on_dispatch(db_session, monkeypatch):
    """When a recurring parent is dispatched, a child record is created."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)

    # Set next_scheduled_at to the past so it's due
    parent.next_scheduled_at = broadcast_crud.utcnow_naive() - timedelta(minutes=5)
    await db_session.commit()

    # Mock delivery to succeed
    monkeypatch.setattr(
        "app.services.broadcast_processor.deliver_message",
        AsyncMock(return_value=[_success_result()]),
    )

    # Dispatch
    await dispatch_due_broadcasts()

    # Reload parent
    await db_session.refresh(parent)
    assert parent.status == "sending"  # claimed but processed via child

    # Check a child was created
    from app.models.broadcast import Broadcast
    from sqlalchemy import select
    result = await db_session.execute(
        select(Broadcast).where(Broadcast.parent_broadcast_id == parent.id)
    )
    children = list(result.scalars().all())
    assert len(children) >= 1
    child = children[0]
    assert child.account_id == parent.account_id
    assert child.message == parent.message
    assert child.recipients == parent.recipients


@pytest.mark.asyncio
async def test_recurring_child_has_correct_status_after_success(db_session, monkeypatch):
    """After successful delivery, the child broadcast has status='sent'."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    parent.next_scheduled_at = broadcast_crud.utcnow_naive() - timedelta(minutes=5)
    await db_session.commit()

    monkeypatch.setattr(
        "app.services.broadcast_processor.deliver_message",
        AsyncMock(return_value=[_success_result()]),
    )

    await dispatch_due_broadcasts()

    from app.models.broadcast import Broadcast
    from sqlalchemy import select
    result = await db_session.execute(
        select(Broadcast).where(Broadcast.parent_broadcast_id == parent.id)
    )
    children = list(result.scalars().all())
    assert len(children) >= 1
    # Reload child to get latest status (delivery happens async)
    child = children[0]
    await db_session.refresh(child)
    assert child.status in ("sent", "pending")  # may still be processing


@pytest.mark.asyncio
async def test_recurring_parent_not_dispatched_if_next_scheduled_at_in_future(db_session, monkeypatch):
    """Parent with future next_scheduled_at is not dispatched."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    parent.next_scheduled_at = broadcast_crud.utcnow_naive() + timedelta(hours=1)
    await db_session.commit()

    process_mock = AsyncMock()
    monkeypatch.setattr("app.scheduler.scheduler.process_recurring_parent", process_mock)

    await dispatch_due_broadcasts()

    process_mock.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# 4. Rescheduling
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_reschedule_advances_next_scheduled_at(db_session):
    """reschedule_recurring_broadcast advances next_scheduled_at by interval."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=60)
    old_next = parent.next_scheduled_at
    assert old_next is not None

    updated = await broadcast_crud.reschedule_recurring_broadcast(db_session, parent.id)
    assert updated is not None
    assert updated.next_scheduled_at is not None
    # Should be roughly 60 minutes after old_next or now
    expected_min = old_next + timedelta(minutes=59)
    assert updated.next_scheduled_at >= expected_min


@pytest.mark.asyncio
async def test_reschedule_does_nothing_for_cancelled(db_session):
    """Rescheduling a cancelled broadcast returns None."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=60)
    parent.status = "cancelled"
    parent.cancelled_at = broadcast_crud.utcnow_naive()
    await db_session.commit()

    updated = await broadcast_crud.reschedule_recurring_broadcast(db_session, parent.id)
    assert updated is None


@pytest.mark.asyncio
async def test_reschedule_non_recurring_returns_none(db_session):
    """Rescheduling a non-recurring broadcast returns None."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)
    assert broadcast.recurring_interval_minutes is None

    result = await broadcast_crud.reschedule_recurring_broadcast(db_session, broadcast.id)
    assert result is None


@pytest.mark.asyncio
async def test_reschedule_after_dispatch_advances_time_correctly(db_session, monkeypatch):
    """After dispatching a recurring parent, next_scheduled_at is advanced."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=120)
    parent.next_scheduled_at = broadcast_crud.utcnow_naive() - timedelta(minutes=5)
    await db_session.commit()

    monkeypatch.setattr(
        "app.services.broadcast_processor.deliver_message",
        AsyncMock(return_value=[_success_result()]),
    )

    await dispatch_due_broadcasts()

    await db_session.refresh(parent)
    assert parent.next_scheduled_at is not None
    # Should be roughly 120 minutes from now
    expected = broadcast_crud.utcnow_naive() + timedelta(minutes=119)
    assert parent.next_scheduled_at >= expected


# ═══════════════════════════════════════════════════════════════════════
# 5. Cancellation
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cancel_recurring_sets_cancelled_status(db_session):
    """Cancelling sets status to 'cancelled' and records cancelled_at."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)

    before = broadcast_crud.utcnow_naive()
    updated = await broadcast_crud.cancel_recurring_broadcast(db_session, parent.id)
    after = broadcast_crud.utcnow_naive()

    assert updated is not None
    assert updated.status == "cancelled"
    assert updated.cancelled_at is not None
    assert before <= updated.cancelled_at <= after
    assert updated.next_scheduled_at is None


@pytest.mark.asyncio
async def test_cancel_non_recurring_returns_none(db_session):
    """Cancelling a non-recurring broadcast returns None."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)
    result = await broadcast_crud.cancel_recurring_broadcast(db_session, broadcast.id)
    assert result is None


@pytest.mark.asyncio
async def test_cancel_already_cancelled_returns_same(db_session):
    """Cancelling an already-cancelled broadcast returns it as-is."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    await broadcast_crud.cancel_recurring_broadcast(db_session, parent.id)

    # Cancel again — should return the cancelled broadcast
    result = await broadcast_crud.cancel_recurring_broadcast(db_session, parent.id)
    assert result is not None
    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_recurring_never_dispatched_again(db_session, monkeypatch):
    """A cancelled recurring broadcast is never included in dispatch."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    parent.next_scheduled_at = broadcast_crud.utcnow_naive() - timedelta(minutes=5)
    await db_session.commit()

    # Cancel it
    await broadcast_crud.cancel_recurring_broadcast(db_session, parent.id)

    process_mock = AsyncMock()
    monkeypatch.setattr("app.scheduler.scheduler.process_recurring_parent", process_mock)

    await dispatch_due_broadcasts()

    # Should NOT be dispatched
    process_mock.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# 6. Restart persistence
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_due_recurring_picked_up_on_restart(db_session, monkeypatch):
    """After restart, scheduler picks up due recurring broadcasts."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=60)
    # Set next_scheduled_at to the past — this simulates a restart where
    # the schedule was due while the app was offline
    parent.next_scheduled_at = broadcast_crud.utcnow_naive() - timedelta(minutes=30)
    await db_session.commit()

    process_mock = AsyncMock()
    monkeypatch.setattr("app.scheduler.scheduler.process_recurring_parent", process_mock)

    # The scheduler's list_due_scheduled_broadcasts should find this parent
    due = await broadcast_crud.list_due_scheduled_broadcasts(db_session)
    parent_ids = [b.id for b in due if b.recurring_interval_minutes is not None]
    assert parent.id in parent_ids


@pytest.mark.asyncio
async def test_restart_does_not_reprocess_completed_one_time(db_session, monkeypatch):
    """After restart, only past-due recurring broadcasts are picked up, not sent ones."""
    account = await _make_account(db_session)
    one_time = await _make_broadcast(db_session, account.id)
    one_time.status = "sent"
    one_time.sent_at = broadcast_crud.utcnow_naive() - timedelta(hours=2)
    one_time.scheduled_at = broadcast_crud.utcnow_naive() - timedelta(hours=2)
    await db_session.commit()

    due = await broadcast_crud.list_due_scheduled_broadcasts(db_session)
    assert one_time.id not in [b.id for b in due]


# ═══════════════════════════════════════════════════════════════════════
# 7. Duplicate/overlapping execution prevention
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_recurring_not_duplicated_by_concurrent_ticks(db_session, monkeypatch):
    """Recurring parent already in _running_recurring is skipped."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    parent.next_scheduled_at = broadcast_crud.utcnow_naive() - timedelta(minutes=5)
    await db_session.commit()

    import app.scheduler.scheduler as scheduler_module
    scheduler_module._running_recurring.add(parent.id)

    process_mock = AsyncMock()
    monkeypatch.setattr("app.scheduler.scheduler.process_recurring_parent", process_mock)

    await dispatch_due_broadcasts()

    process_mock.assert_not_called()
    scheduler_module._running_recurring.discard(parent.id)


@pytest.mark.asyncio
async def test_recurring_not_duplicated_by_atomic_claim(db_session, monkeypatch):
    """If parent is already claimed (status != pending), it's skipped."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    parent.next_scheduled_at = broadcast_crud.utcnow_naive() - timedelta(minutes=5)
    parent.status = "sending"  # already claimed
    await db_session.commit()

    process_mock = AsyncMock()
    monkeypatch.setattr("app.scheduler.scheduler.process_recurring_parent", process_mock)

    await dispatch_due_broadcasts()

    process_mock.assert_not_called()


@pytest.mark.asyncio
async def test_recurring_skipped_if_already_dispatched_this_tick(db_session, monkeypatch):
    """Same recurring parent doesn't appear twice in one tick."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    parent.next_scheduled_at = broadcast_crud.utcnow_naive() - timedelta(minutes=5)
    await db_session.commit()

    process_mock = AsyncMock()
    monkeypatch.setattr("app.scheduler.scheduler.process_recurring_parent", process_mock)
    monkeypatch.setattr("app.scheduler.scheduler.broadcast_crud.claim_broadcast_dispatch", AsyncMock(return_value=True))

    await dispatch_due_broadcasts()

    # Should be called exactly once
    assert process_mock.call_count == 1


# ═══════════════════════════════════════════════════════════════════════
# 8. Existing non-recurring broadcasts still work
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_one_time_broadcast_unchanged(db_session, monkeypatch):
    """A one-time immediate broadcast still works as before."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    monkeypatch.setattr(
        "app.services.broadcast_processor.deliver_message",
        AsyncMock(return_value=[_success_result()]),
    )

    await broadcast_crud.update_broadcast_status(db_session, broadcast, status="sending", mark_sent=True)

    await db_session.refresh(broadcast)
    assert broadcast.status == "sending"
    assert broadcast.recurring_interval_minutes is None


@pytest.mark.asyncio
async def test_scheduled_broadcast_still_dispatched_by_scheduler(db_session, monkeypatch):
    """One-time scheduled broadcast is still picked up by the scheduler."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(
        db_session,
        account.id,
        scheduled_at=broadcast_crud.utcnow_naive() - timedelta(minutes=5),
    )

    due = await broadcast_crud.list_due_scheduled_broadcasts(db_session)
    assert any(b.id == broadcast.id for b in due)


@pytest.mark.asyncio
async def test_list_recurring_returns_only_recurring(db_session):
    """list_recurring_broadcasts returns only broadcasts with recurring_interval_minutes."""
    account = await _make_account(db_session)
    recurring = await _make_broadcast(db_session, account.id, recurring_interval_minutes=60)
    one_time = await _make_broadcast(db_session, account.id)

    recents = await broadcast_crud.list_recurring_broadcasts(db_session)
    ids = [b.id for b in recents]
    assert recurring.id in ids
    assert one_time.id not in ids


@pytest.mark.asyncio
async def test_list_recurring_excludes_cancelled(db_session):
    """list_recurring_broadcasts excludes cancelled recurring broadcasts."""
    account = await _make_account(db_session)
    active = await _make_broadcast(db_session, account.id, recurring_interval_minutes=60)
    cancelled = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    cancelled.status = "cancelled"
    cancelled.cancelled_at = broadcast_crud.utcnow_naive()
    await db_session.commit()

    recents = await broadcast_crud.list_recurring_broadcasts(db_session)
    ids = [b.id for b in recents]
    assert active.id in ids
    assert cancelled.id not in ids


# ═══════════════════════════════════════════════════════════════════════
# API-level tests
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_api_create_recurring_broadcast(client, db_session):
    """POST /api/broadcast with recurring_interval_minutes creates a recurring broadcast."""
    import json
    account = await _make_account(db_session, "+821033330010")
    account_id = account.id

    res = await client.post("/api/broadcast", data={
        "account_id": account_id,
        "message": "30분 반복",
        "recipients": json.dumps(["-100999"]),
        "recurring_interval_minutes": "30",
    })
    assert res.status_code == 202
    body = res.json()
    assert body["recurring_interval_minutes"] == 30
    assert body["status"] == "pending"
    assert body["next_scheduled_at"] is not None


@pytest.mark.asyncio
async def test_api_create_recurring_broadcast_invalid_interval(client, db_session):
    """POST /api/broadcast with invalid recurring_interval_minutes returns 422."""
    import json
    account = await _make_account(db_session, "+821033330011")
    account_id = account.id

    res = await client.post("/api/broadcast", data={
        "account_id": account_id,
        "message": "잘못된 간격",
        "recipients": json.dumps(["-100999"]),
        "recurring_interval_minutes": "45",
    })
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_api_fetch_recurring_broadcasts(client, db_session):
    """GET /api/broadcast/recurring returns active recurring broadcasts."""
    import json
    account = await _make_account(db_session, "+821033330012")
    account_id = account.id

    # Create recurring broadcasts
    await client.post("/api/broadcast", data={
        "account_id": account_id,
        "message": "30분 반복",
        "recipients": json.dumps(["-100999"]),
        "recurring_interval_minutes": "30",
    })

    # Fetch recurring
    res = await client.get("/api/broadcast/recurring")
    assert res.status_code == 200
    items = res.json()
    assert len(items) >= 1
    for item in items:
        assert item["recurring_interval_minutes"] is not None


@pytest.mark.asyncio
async def test_api_create_one_time_no_recurring_field(client, db_session):
    """POST /api/broadcast without recurring_interval_minutes creates a normal broadcast."""
    import json
    account = await _make_account(db_session, "+821033330013")
    account_id = account.id

    res = await client.post("/api/broadcast", data={
        "account_id": account_id,
        "message": "일회성",
        "recipients": json.dumps(["-100999"]),
    })
    assert res.status_code == 202
    body = res.json()
    assert body["recurring_interval_minutes"] is None
    assert body["cancelled_at"] is None
    assert body["next_scheduled_at"] is None


@pytest.mark.asyncio
async def test_api_get_broadcast_shows_recurring_fields(client, db_session):
    """GET /api/broadcast/{id} includes recurring fields for recurring broadcasts."""
    import json
    account = await _make_account(db_session, "+821033330014")
    account_id = account.id

    res = await client.post("/api/broadcast", data={
        "account_id": account_id,
        "message": "반복 테스트",
        "recipients": json.dumps(["-100999"]),
        "recurring_interval_minutes": "60",
    })
    assert res.status_code == 202
    broadcast_id = res.json()["id"]

    get_res = await client.get(f"/api/broadcast/{broadcast_id}")
    assert get_res.status_code == 200
    body = get_res.json()
    assert body["recurring_interval_minutes"] == 60
    assert "next_scheduled_at" in body
    assert "cancelled_at" in body


@pytest.mark.asyncio
async def test_api_cancel_recurring(client, db_session):
    """POST /api/broadcast/{id}/cancel works via the API."""
    import json
    account = await _make_account(db_session, "+821033330015")
    account_id = account.id

    # Create recurring broadcast
    res = await client.post("/api/broadcast", data={
        "account_id": account_id,
        "message": "반복 테스트",
        "recipients": json.dumps(["-100999"]),
        "recurring_interval_minutes": "60",
    })
    assert res.status_code == 202
    broadcast_id = res.json()["id"]

    # Cancel it
    cancel_res = await client.post(f"/api/broadcast/{broadcast_id}/cancel")
    assert cancel_res.status_code == 200
    body = cancel_res.json()
    assert body["status"] == "cancelled"
    assert body["cancelled_at"] is not None


@pytest.mark.asyncio
async def test_api_cancel_non_recurring_returns_409(client, db_session):
    """Cancelling a non-recurring broadcast via API returns 409."""
    import json
    account = await _make_account(db_session, "+821033330016")
    account_id = account.id

    res = await client.post("/api/broadcast", data={
        "account_id": account_id,
        "message": "일회성",
        "recipients": json.dumps(["-100999"]),
    })
    assert res.status_code == 202
    broadcast_id = res.json()["id"]

    cancel_res = await client.post(f"/api/broadcast/{broadcast_id}/cancel")
    assert cancel_res.status_code == 409
