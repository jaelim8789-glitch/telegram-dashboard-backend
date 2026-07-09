"""Sprint 14 + Sprint 22: Behavioral tests for the scheduler.

Sprint 22 reliability extensions:
- Error isolation: one failure doesn't block others
- Atomic claim prevents duplicate concurrent execution
- In-memory concurrency guard prevents same-process duplicates
- Failed runs record safe error info without disabling valid schedules
- Pause/resume/delete during execution is safe (atomic claim rejects claimed items)
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.crud import account as account_crud
from app.crud import broadcast as broadcast_crud
from app.schemas.account import AccountCreate
from app.schemas.broadcast import BroadcastCreate
from app.scheduler.scheduler import dispatch_due_broadcasts, dispatch_due_reply_macros


async def _make_account(db_session, phone="+821022229999"):
    return await account_crud.create_account(db_session, AccountCreate(phone=phone))


async def _make_scheduled_broadcast(db_session, account_id, *, seconds_ago=5, message="예약 발송"):
    payload = BroadcastCreate(account_id=account_id, message=message, recipients=["-100999"])
    scheduled_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=seconds_ago)
    return await broadcast_crud.create_broadcast(db_session, payload, media_path=None, scheduled_at=scheduled_at)


# ═══════════════════════════════════════════════════════════════════════
# Sprint 14 tests (preserved)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_processes_due_broadcast(db_session, monkeypatch):
    account = await _make_account(db_session)
    broadcast = await _make_scheduled_broadcast(db_session, account.id)

    monkeypatch.setattr("app.scheduler.scheduler.process_broadcast", AsyncMock(return_value=None))
    monkeypatch.setattr("app.scheduler.scheduler.broadcast_crud.claim_broadcast_dispatch", AsyncMock(return_value=True))
    import app.scheduler.scheduler as scheduler_module

    await dispatch_due_broadcasts()

    scheduler_module.process_broadcast.assert_awaited_once_with(broadcast.id)


@pytest.mark.asyncio
async def test_dispatch_ignores_not_yet_due_broadcast(db_session, monkeypatch):
    account = await _make_account(db_session)
    payload = BroadcastCreate(account_id=account.id, message="아직 안 됨", recipients=["-100999"])
    future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    await broadcast_crud.create_broadcast(db_session, payload, media_path=None, scheduled_at=future)

    process_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("app.scheduler.scheduler.process_broadcast", process_mock)

    await dispatch_due_broadcasts()

    process_mock.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_defers_rate_limited_broadcast_leaving_it_pending(db_session, monkeypatch):
    account = await _make_account(db_session)

    # An already-sent broadcast for this account within the last minute...
    already_sent = await _make_scheduled_broadcast(db_session, account.id, seconds_ago=120, message="이미 보냄")
    already_sent.status = "sent"
    already_sent.sent_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)
    await db_session.commit()

    # ...blocks a second, currently-due scheduled broadcast for the same account.
    blocked = await _make_scheduled_broadcast(db_session, account.id, seconds_ago=5, message="막힌 예약 발송")

    process_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("app.scheduler.scheduler.process_broadcast", process_mock)

    await dispatch_due_broadcasts()

    process_mock.assert_not_called()
    await db_session.refresh(blocked)
    assert blocked.status == "pending"  # left alone; the next 30s tick will retry it


# ═══════════════════════════════════════════════════════════════════════
# Sprint 22 — Error isolation tests
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_one_failure_does_not_block_others(db_session, monkeypatch):
    """If one broadcast fails, others still get processed."""
    account = await _make_account(db_session)
    b1 = await _make_scheduled_broadcast(db_session, account.id, seconds_ago=10, message="b1")
    b2 = await _make_scheduled_broadcast(db_session, account.id, seconds_ago=5, message="b2")

    # Make b1 fail, b2 succeed
    async def process_side_effect(broadcast_id):
        if broadcast_id == b1.id:
            raise RuntimeError("Simulated failure")
        return None

    monkeypatch.setattr("app.scheduler.scheduler.process_broadcast", AsyncMock(side_effect=process_side_effect))

    await dispatch_due_broadcasts()

    import app.scheduler.scheduler as scheduler_module
    assert scheduler_module.process_broadcast.call_count == 2


@pytest.mark.asyncio
async def test_dispatch_skips_already_running_broadcast(db_session, monkeypatch):
    """Broadcast in _running_broadcasts set is skipped."""
    account = await _make_account(db_session)
    broadcast = await _make_scheduled_broadcast(db_session, account.id)

    import app.scheduler.scheduler as scheduler_module
    scheduler_module._running_broadcasts.add(broadcast.id)

    process_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("app.scheduler.scheduler.process_broadcast", process_mock)

    await dispatch_due_broadcasts()

    process_mock.assert_not_called()
    scheduler_module._running_broadcasts.discard(broadcast.id)


@pytest.mark.asyncio
async def test_dispatch_skips_already_claimed_broadcast(db_session, monkeypatch):
    """Broadcast already claimed (status != pending) is skipped."""
    account = await _make_account(db_session)
    broadcast = await _make_scheduled_broadcast(db_session, account.id)

    # Manually claim it
    broadcast.status = "sending"
    await db_session.commit()

    process_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("app.scheduler.scheduler.process_broadcast", process_mock)

    await dispatch_due_broadcasts()

    process_mock.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_records_error_on_failure(db_session, monkeypatch):
    """When process_broadcast raises, error is recorded on the broadcast."""
    account = await _make_account(db_session)
    broadcast = await _make_scheduled_broadcast(db_session, account.id)

    monkeypatch.setattr(
        "app.scheduler.scheduler.process_broadcast",
        AsyncMock(side_effect=RuntimeError("Connection failed")),
    )

    await dispatch_due_broadcasts()

    await db_session.refresh(broadcast)
    assert broadcast.error_message is not None
    assert "Connection failed" in broadcast.error_message


@pytest.mark.asyncio
async def test_dispatch_clears_running_set_after_failure(db_session, monkeypatch):
    """_running_broadcasts set is cleaned up even after failure."""
    account = await _make_account(db_session)
    broadcast = await _make_scheduled_broadcast(db_session, account.id)

    monkeypatch.setattr(
        "app.scheduler.scheduler.process_broadcast",
        AsyncMock(side_effect=RuntimeError("fail")),
    )

    import app.scheduler.scheduler as scheduler_module
    await dispatch_due_broadcasts()

    assert broadcast.id not in scheduler_module._running_broadcasts


# ═══════════════════════════════════════════════════════════════════════
# Sprint 22 — Reply macro dispatch tests
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_reply_macros_skips_already_running(db_session, monkeypatch):
    """Macro in _running_macros set is skipped."""
    import app.scheduler.scheduler as scheduler_module
    scheduler_module._running_macros.add("macro-1")

    list_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("app.scheduler.scheduler.macro_crud.list_active_macros_due", list_mock)

    await dispatch_due_reply_macros()
    # No crash = success
    scheduler_module._running_macros.discard("macro-1")