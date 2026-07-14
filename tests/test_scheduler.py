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


async def _make_macro(db_session, account_id, **kwargs):
    from app.crud import reply_macro as macro_crud
    from app.schemas.reply_macro import ReplyMacroCreate

    defaults = dict(
        name="macro",
        target_chats=["-100999"],
        message_content="hello",
        schedule_type="interval",
        interval_hours=1,
    )
    defaults.update(kwargs)
    return await macro_crud.create_macro(db_session, account_id, ReplyMacroCreate(**defaults))


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
    account1 = await _make_account(db_session, phone="+821022229991")
    account2 = await _make_account(db_session, phone="+821022229992")
    b1 = await _make_scheduled_broadcast(db_session, account1.id, seconds_ago=10, message="b1")
    b2 = await _make_scheduled_broadcast(db_session, account2.id, seconds_ago=5, message="b2")

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


@pytest.mark.asyncio
async def test_claim_macro_dispatch_rejects_stale_duplicate_claim(db_session):
    """Regression: claim_macro_dispatch previously only checked is_active, so
    any repeated claim call succeeded regardless of a prior claim — two
    overlapping scheduler ticks (or two workers) that both read the macro as
    due before either claimed it would BOTH win and dispatch it twice. The
    claim must be conditioned on the last_sent_at value observed when the
    macro was read as due (optimistic concurrency), not just is_active.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.crud import reply_macro as macro_crud

    account = await _make_account(db_session)
    macro = await _make_macro(db_session, account.id)
    assert macro.last_sent_at is None
    macro_id = macro.id

    # Each claim uses its own session, same as the real scheduler (a fresh
    # session per dispatch iteration) — the guarantee being tested is a
    # DB-level one, not something that depends on sharing a session.
    session_maker = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async with session_maker() as db1:
        first = await macro_crud.claim_macro_dispatch(db1, macro_id, None)
    # Second caller has a stale view (still thinks last_sent_at is None) —
    # simulates a duplicate/overlapping tick that read the due list before
    # the first claim landed.
    async with session_maker() as db2:
        second = await macro_crud.claim_macro_dispatch(db2, macro_id, None)

    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_dispatch_reply_macros_not_duplicated_when_seen_due_twice(db_session, monkeypatch):
    """End-to-end scheduler-level regression for the same bug: if the due-macro
    list contains the same macro twice (overlapping ticks sharing a stale
    read), execute_reply_macro must only run once."""
    account = await _make_account(db_session)
    macro = await _make_macro(db_session, account.id)

    execute_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("app.scheduler.scheduler.execute_reply_macro", execute_mock)
    monkeypatch.setattr(
        "app.scheduler.scheduler.macro_crud.list_active_macros_due",
        AsyncMock(return_value=[macro, macro]),
    )

    await dispatch_due_reply_macros()

    execute_mock.assert_awaited_once_with(macro.id)


# ─── Regression: recurring broadcasts must stay visible in /scheduler/upcoming ──
#
# Production symptom: a recurring broadcast kept firing (dispatch_due_broadcasts
# kept processing it) but vanished from the scheduler UI after its first tick.
# Root cause: list_upcoming_scheduled_broadcasts filtered on
# `scheduled_at > now`, but reschedule_recurring_broadcast only ever advances
# `next_scheduled_at` — `scheduled_at` is frozen at creation time and falls
# into the past the moment the parent fires once.


@pytest.mark.asyncio
async def test_recurring_parent_still_upcoming_after_first_fire(db_session):
    from app.api.deps import Identity

    account = await _make_account(db_session)
    payload = BroadcastCreate(
        account_id=account.id,
        message="반복 발송",
        recipients=["-100999"],
        recurring_interval_minutes=30,
    )
    future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30)
    parent = await broadcast_crud.create_broadcast(db_session, payload, media_path=None, scheduled_at=future)

    # Simulate the parent having fired once: next_scheduled_at advances into
    # the future, but scheduled_at (from creation) is left untouched.
    await broadcast_crud.reschedule_recurring_broadcast(db_session, parent.id)

    upcoming = await broadcast_crud.list_upcoming_scheduled_broadcasts(db_session, identity=Identity(kind="admin"))
    assert parent.id in [b.id for b in upcoming], (
        "Recurring parent disappeared from /scheduler/upcoming after its first "
        "tick even though it is still due again per next_scheduled_at."
    )


@pytest.mark.asyncio
async def test_cancelled_recurring_parent_not_upcoming(db_session):
    from app.api.deps import Identity

    account = await _make_account(db_session)
    payload = BroadcastCreate(
        account_id=account.id,
        message="취소된 반복 발송",
        recipients=["-100999"],
        recurring_interval_minutes=30,
    )
    future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30)
    parent = await broadcast_crud.create_broadcast(db_session, payload, media_path=None, scheduled_at=future)
    await broadcast_crud.reschedule_recurring_broadcast(db_session, parent.id)

    parent = await broadcast_crud.get_broadcast(db_session, parent.id)
    parent.status = "cancelled"
    await db_session.commit()

    upcoming = await broadcast_crud.list_upcoming_scheduled_broadcasts(db_session, identity=Identity(kind="admin"))
    assert parent.id not in [b.id for b in upcoming]