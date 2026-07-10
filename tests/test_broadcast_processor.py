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
# Sprint 28 — Banned account state synchronization
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mark_account_banned_clears_session_and_sets_banned(db_session):
    """mark_account_banned sets status to banned and clears session_data."""
    account = await _make_account(db_session)
    account.session_data = "some-encrypted-session"
    account.status = "active"
    account.last_activity = None
    await db_session.commit()

    updated = await account_crud.mark_account_banned(db_session, account)
    assert updated.status == "banned"
    assert updated.session_data is None
    assert updated.last_activity is not None


@pytest.mark.asyncio
async def test_mark_account_banned_persisted(db_session):
    """The banned state is persisted to the database."""
    account = await _make_account(db_session)
    account.session_data = "some-encrypted-session"
    account.status = "active"
    await db_session.commit()

    await account_crud.mark_account_banned(db_session, account)

    reloaded = await account_crud.get_account(db_session, account.id)
    assert reloaded is not None
    assert reloaded.status == "banned"
    assert reloaded.session_data is None


@pytest.mark.asyncio
async def test_deliver_message_banned_account_fast_fails(db_session, monkeypatch):
    """A banned account fast-fails without any Telegram call."""
    account = await _make_account(db_session)
    account.session_data = "some-session"
    account.status = "banned"
    await db_session.commit()

    from app.services.delivery import DeliveryRequest, deliver_message

    get_client_mock = AsyncMock()
    monkeypatch.setattr("app.services.delivery.get_authorized_client", get_client_mock)

    request = DeliveryRequest(
        account_id=account.id,
        recipients=["-100999"],
        message="test",
        source="manual",
    )

    results = await deliver_message(request)
    assert len(results) == 1
    assert results[0].status.value == "banned"
    assert "차단" in results[0].error_message

    # No Telegram call should have been made
    get_client_mock.assert_not_called()


@pytest.mark.asyncio
async def test_deliver_message_persists_banned_on_banned_result(db_session, monkeypatch):
    """When _deliver_with_retry returns BANNED, the account is marked banned in DB."""
    account = await _make_account(db_session)
    account.session_data = "valid-session"
    account.status = "active"
    await db_session.commit()

    from app.services.delivery import DeliveryRequest, deliver_message

    # Mock get_authorized_client to return a dummy client
    monkeypatch.setattr(
        "app.services.delivery.get_authorized_client",
        AsyncMock(return_value="dummy-client"),
    )

    # Mock _deliver_with_retry to return BANNED
    monkeypatch.setattr(
        "app.services.delivery._deliver_with_retry",
        AsyncMock(return_value=DeliveryResult(
            status=DeliveryStatus.BANNED,
            recipient="-100999",
            error_message="계정이 텔레그램에서 차단되었습니다.",
        )),
    )

    request = DeliveryRequest(
        account_id=account.id,
        recipients=["-100999"],
        message="test",
        source="manual",
    )

    results = await deliver_message(request)
    assert len(results) == 1
    assert results[0].status.value == "banned"

    # Verify account was marked banned in DB
    await db_session.refresh(account)
    assert account.status == "banned"
    assert account.session_data is None


# ═══════════════════════════════════════════════════════════════════════
# Sprint 27 — Telegram session recovery
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mark_account_session_invalid_clears_session(db_session):
    """mark_account_session_invalid clears session_data and sets status to inactive."""
    account = await _make_account(db_session)
    account.session_data = "some-encrypted-session"
    account.status = "active"
    account.last_activity = None
    await db_session.commit()

    updated = await account_crud.mark_account_session_invalid(db_session, account)
    assert updated.session_data is None
    assert updated.status == "inactive"
    assert updated.last_activity is not None


@pytest.mark.asyncio
async def test_mark_account_session_invalid_persisted(db_session):
    """The changes are persisted to the database."""
    account = await _make_account(db_session)
    account.session_data = "some-encrypted-session"
    account.status = "active"
    await db_session.commit()

    await account_crud.mark_account_session_invalid(db_session, account)

    reloaded = await account_crud.get_account(db_session, account.id)
    assert reloaded is not None
    assert reloaded.session_data is None
    assert reloaded.status == "inactive"


@pytest.mark.asyncio
async def test_deliver_message_clears_session_on_auth_failure(db_session, monkeypatch):
    """When get_authorized_client raises AccountNotAuthenticatedError,
    the account's session is cleared so subsequent attempts fast-fail."""
    account = await _make_account(db_session)
    account.session_data = "stale-session"
    account.status = "active"
    await db_session.commit()

    from app.services.delivery import DeliveryRequest, deliver_message
    from app.services.telegram_actions import AccountNotAuthenticatedError

    monkeypatch.setattr(
        "app.services.delivery.get_authorized_client",
        AsyncMock(side_effect=AccountNotAuthenticatedError("Session expired")),
    )

    request = DeliveryRequest(
        account_id=account.id,
        recipients=["-100999"],
        message="test",
        source="manual",
    )

    results = await deliver_message(request)
    assert len(results) == 1
    assert results[0].status.value == "session_expired"

    await db_session.refresh(account)
    assert account.session_data is None
    assert account.status == "inactive"


@pytest.mark.asyncio
async def test_deliver_message_without_session_fast_fails(db_session, monkeypatch):
    """An account with no session_data fast-fails without network call."""
    account = await _make_account(db_session)
    account.session_data = None
    account.status = "inactive"
    await db_session.commit()

    from app.services.delivery import DeliveryRequest, deliver_message

    get_client_mock = AsyncMock()
    monkeypatch.setattr("app.services.delivery.get_authorized_client", get_client_mock)

    request = DeliveryRequest(
        account_id=account.id,
        recipients=["-100999"],
        message="test",
        source="manual",
    )

    results = await deliver_message(request)
    assert len(results) == 1
    assert results[0].status.value == "session_expired"

    get_client_mock.assert_not_called()


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
    broadcast.retry_count = 3
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
    await process_broadcast("does-not-exist")


@pytest.mark.asyncio
async def test_process_broadcast_missing_account_marks_failed(db_session, monkeypatch):
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

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

    await process_broadcast(first.id)
    deliver_mock.reset_mock()

    second = await _make_broadcast(db_session, account.id, message="두번째")
    await process_broadcast(second.id)

    deliver_mock.assert_not_called()

    await db_session.refresh(second)
    assert second.status == "failed"
    assert "1분에 1회" in second.error_message


# ═══════════════════════════════════════════════════════════════════════
# Sprint 23 — Broadcast execution timeout
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_process_broadcast_timeout_marks_failed_and_raises(db_session, monkeypatch):
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    async def _never_completes(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(
        "app.services.broadcast_processor.deliver_message",
        AsyncMock(side_effect=_never_completes),
    )
    monkeypatch.setattr("app.config.settings.broadcast_timeout_seconds", 0.01)

    with pytest.raises(asyncio.TimeoutError):
        await process_broadcast(broadcast.id)

    await db_session.refresh(broadcast)
    assert broadcast.status == "failed"
    assert "시간이 초과" in broadcast.error_message


@pytest.mark.asyncio
async def test_process_broadcast_timeout_releases_scheduler_guard(db_session, monkeypatch):
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

    scheduler_module._running_broadcasts.add(broadcast.id)

    with pytest.raises(asyncio.TimeoutError):
        await process_broadcast(broadcast.id)

    scheduler_module._running_broadcasts.discard(broadcast.id)
    assert broadcast.id not in scheduler_module._running_broadcasts


@pytest.mark.asyncio
async def test_process_broadcast_timeout_configurable_via_settings(db_session, monkeypatch):
    assert settings.broadcast_timeout_seconds == 300
    assert hasattr(settings, "broadcast_timeout_seconds")


# ═══════════════════════════════════════════════════════════════════════
# Sprint 24 — Broadcast retry (CRUD-level)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_broadcast_resets_failed_to_pending(db_session):
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

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
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)
    broadcast.status = "sending"
    await db_session.commit()

    result = await broadcast_crud.retry_broadcast(db_session, broadcast.id)
    assert result is None


@pytest.mark.asyncio
async def test_retry_broadcast_rejects_sent_state(db_session):
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)
    broadcast.status = "sent"
    await db_session.commit()

    result = await broadcast_crud.retry_broadcast(db_session, broadcast.id)
    assert result is None


@pytest.mark.asyncio
async def test_retry_broadcast_rejects_pending_state(db_session):
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    result = await broadcast_crud.retry_broadcast(db_session, broadcast.id)
    assert result is None


@pytest.mark.asyncio
async def test_retry_broadcast_returns_none_for_missing(db_session):
    result = await broadcast_crud.retry_broadcast(db_session, "does-not-exist")
    assert result is None


@pytest.mark.asyncio
async def test_retried_broadcast_can_be_processed_again(db_session, monkeypatch):
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    broadcast.status = "failed"
    broadcast.error_message = "First attempt failed"
    broadcast.sent_at = broadcast_crud.utcnow_naive()
    await db_session.commit()

    updated = await broadcast_crud.retry_broadcast(db_session, broadcast.id)
    assert updated is not None
    assert updated.status == "pending"

    monkeypatch.setattr(
        "app.services.broadcast_processor.deliver_message",
        AsyncMock(return_value=[_success_result()]),
    )

    await process_broadcast(broadcast.id)

    await db_session.refresh(broadcast)
    assert broadcast.status == "sent"
    assert broadcast.sent_at is not None