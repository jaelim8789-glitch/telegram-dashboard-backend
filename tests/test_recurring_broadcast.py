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
9. Stale recurring parent recovery (crash windows)
10. Multi-worker safety findings
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.crud import account as account_crud
from app.crud import broadcast as broadcast_crud
from app.crud.broadcast import RECURRING_STALE_TIMEOUT_SECONDS, recover_stale_recurring_parents
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
    assert RECURRING_INTERVAL_VALUES == {30, 60, 120, 180, 360, 720, 1440}


@pytest.mark.asyncio
async def test_recurring_interval_create_with_valid_value(db_session):
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    assert broadcast.recurring_interval_minutes == 30
    assert broadcast.next_scheduled_at is not None


@pytest.mark.asyncio
async def test_recurring_interval_create_with_zero_is_rejected(db_session):
    account = await _make_account(db_session)
    with pytest.raises(ValueError, match="recurring_interval_minutes must be one of"):
        await _make_broadcast(db_session, account.id, recurring_interval_minutes=0)


@pytest.mark.asyncio
async def test_recurring_interval_create_with_invalid_value_is_rejected(db_session):
    account = await _make_account(db_session)
    with pytest.raises(ValueError, match="recurring_interval_minutes must be one of"):
        await _make_broadcast(db_session, account.id, recurring_interval_minutes=45)


@pytest.mark.asyncio
async def test_recurring_interval_create_with_negative_value_is_rejected(db_session):
    account = await _make_account(db_session)
    with pytest.raises(ValueError, match="recurring_interval_minutes must be one of"):
        await _make_broadcast(db_session, account.id, recurring_interval_minutes=-30)


@pytest.mark.asyncio
async def test_recurring_interval_all_allowed_values(db_session):
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
    account = await _make_account(db_session)
    before = broadcast_crud.utcnow_naive()
    broadcast = await _make_broadcast(db_session, account.id, recurring_interval_minutes=60)
    after = broadcast_crud.utcnow_naive()
    assert broadcast.next_scheduled_at is not None
    assert before <= broadcast.next_scheduled_at <= after


@pytest.mark.asyncio
async def test_recurring_scheduled_sets_next_scheduled_at_to_future(db_session):
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
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    assert broadcast.status == "pending"
    assert broadcast.cancelled_at is None


@pytest.mark.asyncio
async def test_recurring_broadcast_is_recurring_interval_is_set(db_session):
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id, recurring_interval_minutes=720)
    assert broadcast.recurring_interval_minutes == 720

    reloaded = await broadcast_crud.get_broadcast(db_session, broadcast.id)
    assert reloaded is not None
    assert reloaded.recurring_interval_minutes == 720


# ═══════════════════════════════════════════════════════════════════════
# 3. Recurring execution — parent creates children
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_recurring_parent_creates_child_on_dispatch(db_session, monkeypatch):
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)

    parent.next_scheduled_at = broadcast_crud.utcnow_naive() - timedelta(minutes=5)
    await db_session.commit()

    monkeypatch.setattr(
        "app.services.broadcast_processor.deliver_message",
        AsyncMock(return_value=[_success_result()]),
    )

    await dispatch_due_broadcasts()

    await db_session.refresh(parent)
    # The dispatch claim (status="sending") must be released back to "pending"
    # once the child is created and the next occurrence is scheduled — the
    # parent has to remain claimable for the *next* due cycle, or the
    # recurrence stops after this one execution.
    assert parent.status == "pending"

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
    child = children[0]
    await db_session.refresh(child)
    assert child.status in ("sent", "pending")


@pytest.mark.asyncio
async def test_recurring_parent_not_dispatched_if_next_scheduled_at_in_future(db_session, monkeypatch):
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
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=60)
    old_next = parent.next_scheduled_at
    assert old_next is not None

    updated = await broadcast_crud.reschedule_recurring_broadcast(db_session, parent.id)
    assert updated is not None
    assert updated.next_scheduled_at is not None
    expected_min = old_next + timedelta(minutes=59)
    assert updated.next_scheduled_at >= expected_min


@pytest.mark.asyncio
async def test_reschedule_does_nothing_for_cancelled(db_session):
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=60)
    parent.status = "cancelled"
    parent.cancelled_at = broadcast_crud.utcnow_naive()
    await db_session.commit()

    updated = await broadcast_crud.reschedule_recurring_broadcast(db_session, parent.id)
    assert updated is None


@pytest.mark.asyncio
async def test_reschedule_non_recurring_returns_none(db_session):
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)
    assert broadcast.recurring_interval_minutes is None

    result = await broadcast_crud.reschedule_recurring_broadcast(db_session, broadcast.id)
    assert result is None


@pytest.mark.asyncio
async def test_reschedule_after_dispatch_advances_time_correctly(db_session, monkeypatch):
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
    expected = broadcast_crud.utcnow_naive() + timedelta(minutes=119)
    assert parent.next_scheduled_at >= expected


# ═══════════════════════════════════════════════════════════════════════
# 5. Cancellation
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cancel_recurring_sets_cancelled_status(db_session):
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
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)
    result = await broadcast_crud.cancel_recurring_broadcast(db_session, broadcast.id)
    assert result is None


@pytest.mark.asyncio
async def test_cancel_already_cancelled_returns_same(db_session):
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    await broadcast_crud.cancel_recurring_broadcast(db_session, parent.id)

    result = await broadcast_crud.cancel_recurring_broadcast(db_session, parent.id)
    assert result is not None
    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_recurring_never_dispatched_again(db_session, monkeypatch):
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    parent.next_scheduled_at = broadcast_crud.utcnow_naive() - timedelta(minutes=5)
    await db_session.commit()

    await broadcast_crud.cancel_recurring_broadcast(db_session, parent.id)

    process_mock = AsyncMock()
    monkeypatch.setattr("app.scheduler.scheduler.process_recurring_parent", process_mock)

    await dispatch_due_broadcasts()

    process_mock.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# 6. Restart persistence
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_due_recurring_picked_up_on_restart(db_session, monkeypatch):
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=60)
    parent.next_scheduled_at = broadcast_crud.utcnow_naive() - timedelta(minutes=30)
    await db_session.commit()

    process_mock = AsyncMock()
    monkeypatch.setattr("app.scheduler.scheduler.process_recurring_parent", process_mock)

    due = await broadcast_crud.list_due_scheduled_broadcasts(db_session)
    parent_ids = [b.id for b in due if b.recurring_interval_minutes is not None]
    assert parent.id in parent_ids


@pytest.mark.asyncio
async def test_restart_does_not_reprocess_completed_one_time(db_session, monkeypatch):
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
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    parent.next_scheduled_at = broadcast_crud.utcnow_naive() - timedelta(minutes=5)
    parent.status = "sending"
    await db_session.commit()

    process_mock = AsyncMock()
    monkeypatch.setattr("app.scheduler.scheduler.process_recurring_parent", process_mock)

    await dispatch_due_broadcasts()

    process_mock.assert_not_called()


@pytest.mark.asyncio
async def test_recurring_parent_fires_again_on_next_due_cycle(db_session, monkeypatch):
    """Regression: previously claim_broadcast_dispatch set status='sending' and
    nothing ever reset it back to 'pending' after a successful dispatch, so the
    parent's own dispatch claim permanently blocked claim_broadcast_dispatch's
    `WHERE status == 'pending'` check on every later tick — the recurrence
    fired exactly once and then silently stopped forever, even though
    next_scheduled_at kept advancing correctly."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    parent.next_scheduled_at = broadcast_crud.utcnow_naive() - timedelta(minutes=5)
    await db_session.commit()

    monkeypatch.setattr(
        "app.services.broadcast_processor.deliver_message",
        AsyncMock(return_value=[_success_result()]),
    )

    await dispatch_due_broadcasts()
    await db_session.refresh(parent)
    assert parent.status == "pending", "parent must release its claim so the next cycle can dispatch"

    # Simulate the next interval becoming due.
    parent.next_scheduled_at = broadcast_crud.utcnow_naive() - timedelta(minutes=1)
    await db_session.commit()

    await dispatch_due_broadcasts()

    from app.models.broadcast import Broadcast
    from sqlalchemy import select
    result = await db_session.execute(
        select(Broadcast).where(Broadcast.parent_broadcast_id == parent.id)
    )
    children = list(result.scalars().all())
    assert len(children) == 2, "recurring parent must fire again on the next due cycle, not just once"


@pytest.mark.asyncio
async def test_recurring_skipped_if_already_dispatched_this_tick(db_session, monkeypatch):
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    parent.next_scheduled_at = broadcast_crud.utcnow_naive() - timedelta(minutes=5)
    await db_session.commit()

    process_mock = AsyncMock()
    monkeypatch.setattr("app.scheduler.scheduler.process_recurring_parent", process_mock)
    monkeypatch.setattr("app.scheduler.scheduler.broadcast_crud.claim_broadcast_dispatch", AsyncMock(return_value=True))

    await dispatch_due_broadcasts()

    assert process_mock.call_count == 1


# ═══════════════════════════════════════════════════════════════════════
# 8. Existing non-recurring broadcasts still work
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_one_time_broadcast_unchanged(db_session, monkeypatch):
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    await broadcast_crud.update_broadcast_status(db_session, broadcast, status="sending", mark_sent=True)

    await db_session.refresh(broadcast)
    assert broadcast.status == "sending"
    assert broadcast.recurring_interval_minutes is None


@pytest.mark.asyncio
async def test_scheduled_broadcast_still_dispatched_by_scheduler(db_session, monkeypatch):
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
    account = await _make_account(db_session)
    recurring = await _make_broadcast(db_session, account.id, recurring_interval_minutes=60)
    one_time = await _make_broadcast(db_session, account.id)

    recents = await broadcast_crud.list_recurring_broadcasts(db_session)
    ids = [b.id for b in recents]
    assert recurring.id in ids
    assert one_time.id not in ids


@pytest.mark.asyncio
async def test_list_recurring_excludes_cancelled(db_session):
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
# 9. Stale recurring parent recovery
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_recover_stale_recurring_parent(db_session):
    """Recurring parent stuck in 'sending' is recovered to 'pending'."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    parent.status = "sending"
    parent.sent_at = broadcast_crud.utcnow_naive() - timedelta(seconds=RECURRING_STALE_TIMEOUT_SECONDS + 10)
    await db_session.commit()

    recovered = await recover_stale_recurring_parents(db_session)
    assert len(recovered) == 1
    assert recovered[0].id == parent.id
    assert recovered[0].status == "pending"


@pytest.mark.asyncio
async def test_recover_skips_recently_claimed_parent(db_session):
    """Parent claimed recently (within timeout) is NOT recovered."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    parent.status = "sending"
    parent.sent_at = broadcast_crud.utcnow_naive()  # recent
    await db_session.commit()

    recovered = await recover_stale_recurring_parents(db_session)
    assert len(recovered) == 0


@pytest.mark.asyncio
async def test_recover_skips_cancelled_parent(db_session):
    """Cancelled recurring parent in 'sending' is NOT recovered."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    parent.status = "cancelled"
    parent.sent_at = broadcast_crud.utcnow_naive() - timedelta(seconds=RECURRING_STALE_TIMEOUT_SECONDS + 10)
    await db_session.commit()

    recovered = await recover_stale_recurring_parents(db_session)
    assert len(recovered) == 0


@pytest.mark.asyncio
async def test_recover_skips_non_recurring_broadcast(db_session):
    """A normal one-time broadcast in 'sending' is NOT recovered."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)
    broadcast.status = "sending"
    broadcast.sent_at = broadcast_crud.utcnow_naive() - timedelta(seconds=RECURRING_STALE_TIMEOUT_SECONDS + 10)
    await db_session.commit()

    recovered = await recover_stale_recurring_parents(db_session)
    assert len(recovered) == 0


@pytest.mark.asyncio
async def test_recover_cleans_orphan_child(db_session):
    """When a parent is stale with an orphan child, the child is cleaned."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    parent.status = "sending"
    parent.sent_at = broadcast_crud.utcnow_naive() - timedelta(seconds=RECURRING_STALE_TIMEOUT_SECONDS + 10)
    await db_session.commit()

    # Create orphan child (child created but never dispatched before crash)
    now = broadcast_crud.utcnow_naive()
    orphan = await broadcast_crud.create_recurring_child_broadcast(db_session, parent, now)

    recovered = await recover_stale_recurring_parents(db_session)
    assert len(recovered) == 1
    assert recovered[0].id == parent.id
    assert recovered[0].status == "pending"

    # Orphan should be marked failed
    await db_session.refresh(orphan)
    assert orphan.status == "failed"
    assert "복구" in orphan.error_message


@pytest.mark.asyncio
async def test_recover_preserves_already_sent_children(db_session):
    """Already-sent orphan children are NOT modified by recovery."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=30)
    parent.status = "sending"
    parent.sent_at = broadcast_crud.utcnow_naive() - timedelta(seconds=RECURRING_STALE_TIMEOUT_SECONDS + 10)
    await db_session.commit()

    now = broadcast_crud.utcnow_naive()
    orphan = await broadcast_crud.create_recurring_child_broadcast(db_session, parent, now)
    orphan.status = "sent"
    orphan.sent_at = broadcast_crud.utcnow_naive()
    await db_session.commit()

    recovered = await recover_stale_recurring_parents(db_session)
    assert len(recovered) == 1

    await db_session.refresh(orphan)
    assert orphan.status == "sent"  # unchanged


@pytest.mark.asyncio
async def test_recover_restart_recovery(db_session, monkeypatch):
    """After restart, stale parents are recovered and dispatched by scheduler."""
    account = await _make_account(db_session)
    parent = await _make_broadcast(db_session, account.id, recurring_interval_minutes=60)
    parent.status = "sending"
    parent.sent_at = broadcast_crud.utcnow_naive() - timedelta(seconds=200)
    parent.next_scheduled_at = broadcast_crud.utcnow_naive() - timedelta(minutes=5)
    await db_session.commit()

    process_mock = AsyncMock()
    monkeypatch.setattr("app.scheduler.scheduler.process_recurring_parent", process_mock)
    monkeypatch.setattr(
        "app.services.broadcast_processor.deliver_message",
        AsyncMock(return_value=[_success_result()]),
    )

    await dispatch_due_broadcasts()
    assert process_mock.call_count == 1, "Stale parent must be dispatched after restart"


# ═══════════════════════════════════════════════════════════════════════
# 10. Multi-worker safety: stale timeout prevents false recovery
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_stale_timeout_is_larger_than_tick_interval():
    """RECURRING_STALE_TIMEOUT_SECONDS is > 2x DISPATCH_INTERVAL_SECONDS."""
    from app.scheduler.scheduler import DISPATCH_INTERVAL_SECONDS
    assert RECURRING_STALE_TIMEOUT_SECONDS > DISPATCH_INTERVAL_SECONDS * 2


# ═══════════════════════════════════════════════════════════════════════
# Tenant isolation
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_recurring_list_tenant_isolation(db_session):
    """list_recurring_broadcasts with identity scopes to the tenant."""
    acc_a = await _make_account(db_session, "+821033330050")
    acc_a.tenant_id = "tenant-A"
    acc_b = await _make_account(db_session, "+821033330051")
    acc_b.tenant_id = "tenant-B"
    await db_session.commit()

    rec_a = await _make_broadcast(db_session, acc_a.id, recurring_interval_minutes=60)
    rec_b = await _make_broadcast(db_session, acc_b.id, recurring_interval_minutes=60)

    from app.api.deps import Identity

    tenant_a = Identity(kind="user", tenant_id="tenant-A")
    result_a = await broadcast_crud.list_recurring_broadcasts(db_session, identity=tenant_a)
    ids_a = [b.id for b in result_a]
    assert rec_a.id in ids_a, "Tenant A must see its own recurring broadcast"
    assert rec_b.id not in ids_a, "Tenant A must NOT see Tenant B's recurring broadcast"

    result_admin = await broadcast_crud.list_recurring_broadcasts(db_session, identity=Identity(kind="admin"))
    ids_admin = [b.id for b in result_admin]
    assert rec_a.id in ids_admin
    assert rec_b.id in ids_admin

    result_no_tenant = await broadcast_crud.list_recurring_broadcasts(db_session, identity=Identity(kind="api_key", tenant_id=None))
    assert len(result_no_tenant) == 0


@pytest.mark.asyncio
async def test_recurring_logs_tenant_isolation(db_session):
    """list_logs with identity scopes to the tenant when no account_id given."""
    from app.api.deps import Identity

    acc_a = await _make_account(db_session, "+821033330060")
    acc_a.tenant_id = "tenant-A-logs"
    acc_b = await _make_account(db_session, "+821033330061")
    acc_b.tenant_id = "tenant-B-logs"
    await db_session.commit()

    b_a = await _make_broadcast(db_session, acc_a.id)
    b_b = await _make_broadcast(db_session, acc_b.id)

    logs_a = await broadcast_crud.list_logs(db_session, identity=Identity(kind="user", tenant_id="tenant-A-logs"))
    ids_a = [b.id for b in logs_a]
    assert b_a.id in ids_a
    assert b_b.id not in ids_a

    logs_admin = await broadcast_crud.list_logs(db_session, identity=Identity(kind="admin"))
    ids_admin = [b.id for b in logs_admin]
    assert b_a.id in ids_admin
    assert b_b.id in ids_admin

    logs_no_tenant = await broadcast_crud.list_logs(db_session, identity=Identity(kind="api_key", tenant_id=None))
    assert len(logs_no_tenant) == 0


@pytest.mark.asyncio
async def test_recurring_logs_excludes_parents(db_session):
    """list_logs excludes recurring parent records regardless of identity."""
    from app.api.deps import Identity

    account = await _make_account(db_session, "+821033330062")
    recurring = await _make_broadcast(db_session, account.id, recurring_interval_minutes=60)
    one_time = await _make_broadcast(db_session, account.id)

    logs = await broadcast_crud.list_logs(db_session, identity=Identity(kind="admin"))
    ids = [b.id for b in logs]
    assert recurring.id not in ids, "Recurring parent must NOT appear in logs"
    assert one_time.id in ids, "One-time broadcast must appear in logs"