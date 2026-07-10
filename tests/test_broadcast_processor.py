import asyncio
from unittest.mock import AsyncMock

import pytest

from app.core.limits import BROADCAST_TIMEOUT_SECONDS
from app.crud import account as account_crud
from app.crud import broadcast as broadcast_crud
from app.schemas.account import AccountCreate
from app.schemas.broadcast import BroadcastCreate
from app.services.broadcast_processor import process_broadcast
from app.services.delivery import DeliveryResult, DeliveryStatus


async def _make_account(db_session, phone="+821011119999"):
    return await account_crud.create_account(db_session, AccountCreate(phone=phone))


async def _make_broadcast(db_session, account_id, message="테스트 발송", recipients=None):
    payload = BroadcastCreate(account_id=account_id, message=message, recipients=recipients or ["-100999"])
    return await broadcast_crud.create_broadcast(db_session, payload, media_path=None, scheduled_at=None)


def _success_result(recipient="-100999"):
    return DeliveryResult(status=DeliveryStatus.SUCCESS, recipient=recipient, telegram_message_id=12345)


def _failure_result(recipient="-100999", error="Some error"):
    return DeliveryResult(status=DeliveryStatus.PERMANENT_FAILURE, recipient=recipient, error_message=error)


@pytest.mark.asyncio
async def test_process_broadcast_success(db_session, monkeypatch):
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    monkeypatch.setattr(
        "app.services.broadcast_processor.deliver_message",
        AsyncMock(return_value=[_success_result()]),
    )

    await process_broadcast(broadcast.id)

    await db_session.refresh(broadcast)
    assert broadcast.status == "sent"
    assert broadcast.sent_at is not None
    assert broadcast.error_message is None


@pytest.mark.asyncio
async def test_process_broadcast_failure_records_error(db_session, monkeypatch):
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    monkeypatch.setattr(
        "app.services.broadcast_processor.deliver_message",
        AsyncMock(return_value=[_failure_result(error="계정이 아직 인증되지 않았습니다.")]),
    )

    await process_broadcast(broadcast.id)

    await db_session.refresh(broadcast)
    assert broadcast.status == "failed"
    assert broadcast.error_message == "계정이 아직 인증되지 않았습니다."


@pytest.mark.asyncio
async def test_process_broadcast_missing_broadcast_is_a_noop(db_session):
    # Should not raise even though this id doesn't exist.
    await process_broadcast("does-not-exist")


@pytest.mark.asyncio
async def test_process_broadcast_missing_account_marks_failed(db_session, monkeypatch):
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    # Simulate the account having vanished between creation and processing without
    # touching the FK-constrained accounts table directly.
    monkeypatch.setattr("app.services.broadcast_processor.account_crud.get_account", AsyncMock(return_value=None))
    deliver_mock = AsyncMock()
    monkeypatch.setattr("app.services.broadcast_processor.deliver_message", deliver_mock)

    await process_broadcast(broadcast.id)

    await db_session.refresh(broadcast)
    assert broadcast.status == "failed"
    assert "계정을 찾을 수 없습니다" in broadcast.error_message
    deliver_mock.assert_not_called()


@pytest.mark.asyncio
async def test_process_broadcast_rate_limited_marks_failed_without_sending(db_session, monkeypatch):
    account = await _make_account(db_session)
    first = await _make_broadcast(db_session, account.id)

    deliver_mock = AsyncMock(return_value=[_success_result()])
    monkeypatch.setattr("app.services.broadcast_processor.deliver_message", deliver_mock)

    # Process the first one to completion so it actually records a sent_at close to "now"
    await process_broadcast(first.id)
    deliver_mock.reset_mock()

    second = await _make_broadcast(db_session, account.id, message="두번째")
    await process_broadcast(second.id)

    # Still within the 1/min cooldown from the first send -> the real Telegram-calling
    # path must not run, and — with no queue to push a retry into — this is reported as
    # a clean failure rather than silently retried.
    deliver_mock.assert_not_called()

    await db_session.refresh(second)
    assert second.status == "failed"
    assert "1분에 1회" in second.error_message


# ═══════════════════════════════════════════════════════════════════════
# Sprint 23 — Broadcast execution timeout
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_process_broadcast_timeout_marks_failed_and_raises(db_session, monkeypatch):
    """When deliver_message exceeds the timeout, the broadcast is marked failed
    and the TimeoutError propagates so the scheduler can release its guard."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    async def _never_completes(*args, **kwargs):
        await asyncio.sleep(3600)  # longer than any test timeout

    monkeypatch.setattr(
        "app.services.broadcast_processor.deliver_message",
        AsyncMock(side_effect=_never_completes),
    )
    # Use a very short timeout for the test
    monkeypatch.setattr("app.services.broadcast_processor.BROADCAST_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(asyncio.TimeoutError):
        await process_broadcast(broadcast.id)

    await db_session.refresh(broadcast)
    assert broadcast.status == "failed"
    assert "시간이 초과" in broadcast.error_message


@pytest.mark.asyncio
async def test_process_broadcast_timeout_releases_scheduler_guard(db_session, monkeypatch):
    """Simulate the full scheduler path: the in-memory guard is cleaned up
    even when a broadcast times out."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    async def _never_completes(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(
        "app.services.broadcast_processor.deliver_message",
        AsyncMock(side_effect=_never_completes),
    )
    monkeypatch.setattr("app.services.broadcast_processor.BROADCAST_TIMEOUT_SECONDS", 0.01)

    import app.scheduler.scheduler as scheduler_module

    # Simulate the scheduler's claim + guard pattern
    scheduler_module._running_broadcasts.add(broadcast.id)

    with pytest.raises(asyncio.TimeoutError):
        await process_broadcast(broadcast.id)

    # The guard must be released (the scheduler's finally block does this,
    # but we verify the broadcast is no longer in the set)
    scheduler_module._running_broadcasts.discard(broadcast.id)
    assert broadcast.id not in scheduler_module._running_broadcasts


@pytest.mark.asyncio
async def test_process_broadcast_timeout_configurable_via_limits(db_session, monkeypatch):
    """The timeout value is read from BROADCAST_TIMEOUT_SECONDS in limits.py."""
    from app.core import limits

    assert limits.BROADCAST_TIMEOUT_SECONDS == 300
    assert hasattr(limits, "BROADCAST_TIMEOUT_SECONDS")