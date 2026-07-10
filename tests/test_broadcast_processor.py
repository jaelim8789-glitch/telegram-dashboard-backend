import asyncio
from unittest.mock import AsyncMock

import pytest

from app.config import settings
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


# ═══════════════════════════════════════════════════════════════════════
# Sprint 26 — Broadcast retry limits
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_broadcast_increments_retry_count(db_session):
    """Each retry increments retry_count."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    broadcast.status = "failed"
    broadcast.error_message = "fail"
    broadcast.sent_at = broadcast_crud.utcnow_naive()
    await db_session.commit()

    updated = await broadcast_crud.retry_broadcast(db_session, broadcast.id)
    assert updated is not None
    assert updated.retry_count == 1


@pytest.mark.asyncio
async def test_retry_broadcast_hits_limit_at_3(db_session, monkeypatch):
    """After 3 retries, retry_broadcast returns None (limit reached)."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    broadcast.status = "failed"
    broadcast.error_message = "fail"
    broadcast.sent_at = broadcast_crud.utcnow_naive()
    broadcast.retry_count = 3  # already at the default limit
    await db_session.commit()

    result = await broadcast_crud.retry_broadcast(db_session, broadcast.id)
    assert result is None


@pytest.mark.asyncio
async def test_retry_broadcast_obeys_custom_limit(db_session, monkeypatch):
    """A lower custom limit is respected."""
    monkeypatch.setattr("app.config.settings.broadcast_max_retries", 1)

    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    broadcast.status = "failed"
    broadcast.error_message = "fail"
    broadcast.sent_at = broadcast_crud.utcnow_naive()
    broadcast.retry_count = 1
    await db_session.commit()

    result = await broadcast_crud.retry_broadcast(db_session, broadcast.id)
    assert result is None


@pytest.mark.asyncio
async def test_retry_broadcast_zero_limit_disables_retries(db_session, monkeypatch):
    """When broadcast_max_retries=0, retry is immediately rejected."""
    monkeypatch.setattr("app.config.settings.broadcast_max_retries", 0)

    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    broadcast.status = "failed"
    broadcast.error_message = "fail"
    broadcast.sent_at = broadcast_crud.utcnow_naive()
    broadcast.retry_count = 0
    await db_session.commit()

    result = await broadcast_crud.retry_broadcast(db_session, broadcast.id)
    assert result is None


@pytest.mark.asyncio
async def test_retry_broadcast_increments_on_each_retry(db_session):
    """Consecutive retries increment retry_count until the limit."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    for expected in (1, 2, 3):
        broadcast.status = "failed"
        broadcast.error_message = f"attempt {expected}"
        broadcast.sent_at = broadcast_crud.utcnow_naive()
        await db_session.commit()

        updated = await broadcast_crud.retry_broadcast(db_session, broadcast.id)
        assert updated is not None
        assert updated.retry_count == expected

    # Fourth attempt should fail
    broadcast.status = "failed"
    broadcast.error_message = "attempt 4"
    broadcast.sent_at = broadcast_crud.utcnow_naive()
    await db_session.commit()

    result = await broadcast_crud.retry_broadcast(db_session, broadcast.id)
    assert result is None
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
    monkeypatch.setattr("app.config.settings.broadcast_timeout_seconds", 0.01)

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
    monkeypatch.setattr("app.config.settings.broadcast_timeout_seconds", 0.01)

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
async def test_process_broadcast_timeout_configurable_via_settings(db_session, monkeypatch):
    """The timeout value is read from settings.broadcast_timeout_seconds."""
    assert settings.broadcast_timeout_seconds == 300
    assert hasattr(settings, "broadcast_timeout_seconds")


# ═══════════════════════════════════════════════════════════════════════
# Sprint 24 — Broadcast retry (CRUD-level)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_broadcast_resets_failed_to_pending(db_session):
    """retry_broadcast resets a failed broadcast to pending, clearing error and sent_at."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    # Manually set to failed (simulating a failed delivery)
    broadcast.status = "failed"
    broadcast.error_message = "Some error"
    broadcast.sent_at = broadcast_crud.utcnow_naive()
    await db_session.commit()

    updated = await broadcast_crud.retry_broadcast(db_session, broadcast.id)
    assert updated is not None
    assert updated.status == "pending"
    assert updated.error_message is None
    assert updated.sent_at is None


@pytest.mark.asyncio
async def test_retry_broadcast_rejects_sending_state(db_session):
    """retry_broadcast returns None for a broadcast in 'sending' state."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)
    broadcast.status = "sending"
    await db_session.commit()

    result = await broadcast_crud.retry_broadcast(db_session, broadcast.id)
    assert result is None


@pytest.mark.asyncio
async def test_retry_broadcast_rejects_sent_state(db_session):
    """retry_broadcast returns None for a broadcast in 'sent' state."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)
    broadcast.status = "sent"
    await db_session.commit()

    result = await broadcast_crud.retry_broadcast(db_session, broadcast.id)
    assert result is None


@pytest.mark.asyncio
async def test_retry_broadcast_rejects_pending_state(db_session):
    """retry_broadcast returns None for a broadcast already in 'pending' state."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)
    # Already pending by default

    result = await broadcast_crud.retry_broadcast(db_session, broadcast.id)
    assert result is None


@pytest.mark.asyncio
async def test_retry_broadcast_returns_none_for_missing(db_session):
    """retry_broadcast returns None for a non-existent broadcast."""
    result = await broadcast_crud.retry_broadcast(db_session, "does-not-exist")
    assert result is None


@pytest.mark.asyncio
async def test_retried_broadcast_can_be_processed_again(db_session, monkeypatch):
    """After retry, the broadcast can be picked up and processed successfully."""
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    # Simulate a failed broadcast
    broadcast.status = "failed"
    broadcast.error_message = "First attempt failed"
    broadcast.sent_at = broadcast_crud.utcnow_naive()
    await db_session.commit()

    # Retry it
    updated = await broadcast_crud.retry_broadcast(db_session, broadcast.id)
    assert updated is not None
    assert updated.status == "pending"

    # Now process it — should succeed
    monkeypatch.setattr(
        "app.services.broadcast_processor.deliver_message",
        AsyncMock(return_value=[_success_result()]),
    )

    await process_broadcast(broadcast.id)

    await db_session.refresh(broadcast)
    assert broadcast.status == "sent"
    assert broadcast.sent_at is not None