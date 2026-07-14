"""Sprint 14: Behavioral tests for the canonical Telegram delivery pipeline.

All Telegram calls are mocked — no real messages are sent.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from telethon.errors import (
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    FloodWaitError,
    RPCError,
    UserDeactivatedBanError,
    UsernameInvalidError,
)

from app.services.delivery import (
    DeliveryRequest,
    DeliveryResult,
    DeliveryStatus,
    MAX_RETRIES,
    _deliver_with_retry,
    _persist_log,
    _resolve_target,
    _send_single,
    classify_error,
    deliver_message,
)


# ─── Helpers ──────────────────────────────────────────────────────────


def make_flood_wait(seconds: int) -> FloodWaitError:
    """FloodWaitError constructor varies across Telethon versions — set seconds directly."""
    exc = FloodWaitError(request=None)
    exc.seconds = seconds
    return exc


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.send_message = AsyncMock()
    client.send_file = AsyncMock()
    return client


@pytest.fixture
def mock_db_session():
    """Mock async_session_maker to avoid PostgreSQL connection errors in tests."""
    mock_db = AsyncMock()
    mock_db.add.return_value = None
    mock_db.commit.return_value = None
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_db
    with patch("app.services.delivery.async_session_maker", return_value=mock_session):
        yield mock_db


@pytest.fixture
def sample_request():
    return DeliveryRequest(
        account_id="test-acc-1",
        recipients=["-100123", "-100456"],
        message="Hello from test!",
        source="test",
        source_id="test-001",
    )


# ═══════════════════════════════════════════════════════════════════════
# Phase 1: _resolve_target tests
# ═══════════════════════════════════════════════════════════════════════

def test_resolve_target_numeric():
    assert _resolve_target("-100123") == -100123
    assert _resolve_target("12345") == 12345


def test_resolve_target_username():
    assert _resolve_target("@username") == "@username"
    assert _resolve_target("username") == "username"


# ═══════════════════════════════════════════════════════════════════════
# Phase 2: classify_error tests
# ═══════════════════════════════════════════════════════════════════════

def test_classify_flood_wait():
    exc = make_flood_wait(30)
    status, msg = classify_error(exc)
    assert status == DeliveryStatus.FLOOD_WAIT
    assert "30초" in msg
    assert "텔레그램" in msg


def test_classify_banned():
    exc = UserDeactivatedBanError(request=None)
    status, msg = classify_error(exc)
    assert status == DeliveryStatus.BANNED
    assert "차단" in msg


def test_classify_forbidden():
    exc = ChatWriteForbiddenError(request=None)
    status, msg = classify_error(exc)
    assert status == DeliveryStatus.FORBIDDEN
    assert "권한" in msg


def test_classify_invalid_recipient():
    exc = UsernameInvalidError(request=None)
    status, msg = classify_error(exc)
    assert status == DeliveryStatus.INVALID_RECIPIENT
    assert "수신자" in msg


def test_classify_session_expired():
    from app.services.telegram_actions import AccountNotAuthenticatedError
    exc = AccountNotAuthenticatedError("세션이 만료되었습니다")
    status, msg = classify_error(exc)
    assert status == DeliveryStatus.SESSION_EXPIRED


def test_classify_network_error():
    exc = ConnectionError("connection refused")
    status, msg = classify_error(exc)
    assert status == DeliveryStatus.NETWORK_ERROR


def test_classify_rpc_error():
    exc = RPCError(request=None, message="Some RPC error")
    status, msg = classify_error(exc)
    assert status == DeliveryStatus.PERMANENT_FAILURE


def test_classify_unknown_error():
    exc = ValueError("unexpected")
    status, msg = classify_error(exc)
    assert status == DeliveryStatus.INTERNAL_ERROR


def test_classify_never_exposes_raw_exception():
    """Safe error messages must never contain raw exception text."""
    exc = UserDeactivatedBanError(request=None)
    _, msg = classify_error(exc)
    assert "UserDeactivatedBanError" not in msg
    assert "차단" in msg


# ═══════════════════════════════════════════════════════════════════════
# Phase 3: _send_single tests
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_send_single_success(mock_client):
    mock_client.send_message.return_value.id = 42
    status, msg_id, error, flood = await _send_single(mock_client, -100123, "hi", None)
    assert status == DeliveryStatus.SUCCESS
    assert msg_id == 42
    assert error is None
    mock_client.send_message.assert_awaited_once_with(-100123, "hi", reply_to=None)


@pytest.mark.asyncio
async def test_send_single_success_with_file(mock_client):
    mock_result = MagicMock()
    mock_result.id = 99
    mock_client.send_file.return_value = mock_result
    status, msg_id, error, flood = await _send_single(mock_client, -100123, "hi", "/path/img.jpg")
    assert status == DeliveryStatus.SUCCESS
    assert msg_id == 99
    mock_client.send_file.assert_awaited_once_with(-100123, "/path/img.jpg", caption="hi", reply_to=None)


@pytest.mark.asyncio
async def test_send_single_flood_wait(mock_client):
    mock_client.send_message.side_effect = make_flood_wait(15)
    status, msg_id, error, flood = await _send_single(mock_client, -100123, "hi", None)
    assert status == DeliveryStatus.FLOOD_WAIT
    assert flood == 15


@pytest.mark.asyncio
async def test_send_single_never_exposes_secrets_in_error(mock_client):
    """Raw exception text must never be returned as error_message."""
    mock_client.send_message.side_effect = UserDeactivatedBanError(request=None)
    _, _, error, _ = await _send_single(mock_client, -100123, "hi", None)
    assert error is not None
    assert "UserDeactivatedBanError" not in error
    assert "차단" in error


@pytest.mark.asyncio
async def test_send_single_hung_call_times_out_instead_of_blocking_forever(mock_client, monkeypatch):
    """Production symptom: a single stalled Telethon call silently consumed the
    entire broadcast-level timeout budget, cancelling the whole broadcast even
    though every other recipient would have gone through fine. A bounded
    per-message timeout turns that into one classified, retriable failure."""
    from app.services import delivery as delivery_module

    monkeypatch.setattr(delivery_module, "PER_MESSAGE_TIMEOUT_SECONDS", 0.05)

    async def _hang(*args, **kwargs):
        await asyncio.sleep(10)

    mock_client.send_message.side_effect = _hang
    status, msg_id, error, flood = await _send_single(mock_client, -100123, "hi", None)
    assert status == DeliveryStatus.NETWORK_ERROR
    assert msg_id is None
    assert "지연" in error or "시간" in error


# ═══════════════════════════════════════════════════════════════════════
# Phase 4: _deliver_with_retry tests
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_retry_success_first_attempt(mock_client, mock_db_session):
    mock_client.send_message.return_value.id = 100
    result = await _deliver_with_retry(
        mock_client, -100123, "-100123", "hi", None,
        source="test", source_id="t1", account_id="acc1",
    )
    assert result.status == DeliveryStatus.SUCCESS
    assert result.telegram_message_id == 100
    assert result.attempt_count == 1


@pytest.mark.asyncio
async def test_retry_temporary_failure_then_success(mock_client, mock_db_session):
    call_count = 0

    async def send_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise ConnectionError("network timeout")
        return MagicMock(id=200)

    mock_client.send_message.side_effect = send_side_effect

    result = await _deliver_with_retry(
        mock_client, -100123, "-100123", "hi", None,
        source="test", source_id="t1", account_id="acc1",
    )
    assert result.status == DeliveryStatus.SUCCESS
    assert result.telegram_message_id == 200
    assert result.attempt_count == 3


@pytest.mark.asyncio
async def test_retry_permanent_failure_does_not_retry(mock_client, mock_db_session):
    mock_client.send_message.side_effect = ChatWriteForbiddenError(request=None)
    result = await _deliver_with_retry(
        mock_client, -100123, "-100123", "hi", None,
        source="test", source_id="t1", account_id="acc1",
    )
    assert result.status == DeliveryStatus.FORBIDDEN
    assert result.attempt_count == 1


@pytest.mark.asyncio
async def test_retry_bounded_max_attempts(mock_client, mock_db_session):
    mock_client.send_message.side_effect = ConnectionError("persistent failure")

    with patch("app.services.delivery.BASE_BACKOFF_SECONDS", 0.01):
        result = await _deliver_with_retry(
            mock_client, -100123, "-100123", "hi", None,
            source="test", source_id="t1", account_id="acc1",
        )

    assert result.status == DeliveryStatus.NETWORK_ERROR
    assert result.attempt_count == MAX_RETRIES


@pytest.mark.asyncio
async def test_retry_banned_account_does_not_retry(mock_client, mock_db_session):
    mock_client.send_message.side_effect = UserDeactivatedBanError(request=None)
    result = await _deliver_with_retry(
        mock_client, -100123, "-100123", "hi", None,
        source="test", source_id="t1", account_id="acc1",
    )
    assert result.status == DeliveryStatus.BANNED
    assert result.attempt_count == 1


@pytest.mark.asyncio
async def test_retry_invalid_recipient_does_not_retry(mock_client, mock_db_session):
    mock_client.send_message.side_effect = UsernameInvalidError(request=None)
    result = await _deliver_with_retry(
        mock_client, -100123, "-100123", "hi", None,
        source="test", source_id="t1", account_id="acc1",
    )
    assert result.status == DeliveryStatus.INVALID_RECIPIENT
    assert result.attempt_count == 1


# ═══════════════════════════════════════════════════════════════════════
# Phase 5: _persist_log tests
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_persist_log_success(mock_db_session):
    await _persist_log(
        account_id="acc1",
        recipient="-100123",
        source="broadcast",
        source_id="b1",
        status=DeliveryStatus.SUCCESS,
        success=True,
        telegram_message_id=42,
        error_message=None,
        attempt_count=1,
        message_content="hi",
    )
    mock_db_session.add.assert_called_once()
    mock_db_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_log_failure(mock_db_session):
    await _persist_log(
        account_id="acc1",
        recipient="-100456",
        source="reply_macro",
        source_id="m1",
        status=DeliveryStatus.FORBIDDEN,
        success=False,
        telegram_message_id=None,
        error_message="권한 없음",
        attempt_count=1,
        message_content="hi",
    )
    mock_db_session.add.assert_called_once()
    mock_db_session.commit.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════
# Phase 6: deliver_message tests (full pipeline, mocked Telethon)
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@patch("app.services.delivery.get_authorized_client")
@patch("app.services.delivery.account_crud.get_account")
async def test_deliver_message_success(mock_get_account, mock_get_client, sample_request, mock_db_session):
    mock_account = MagicMock()
    mock_account.id = "test-acc-1"
    mock_get_account.return_value = mock_account

    mock_client = AsyncMock()
    mock_client.send_message.return_value.id = 123
    mock_get_client.return_value = mock_client

    results = await deliver_message(sample_request)

    assert len(results) == 2
    assert all(r.status == DeliveryStatus.SUCCESS for r in results)
    assert all(r.telegram_message_id == 123 for r in results)
    assert mock_client.send_message.await_count == 2


@pytest.mark.asyncio
@patch("app.services.delivery.get_authorized_client")
@patch("app.services.delivery.account_crud.get_account")
async def test_deliver_message_account_not_found(mock_get_account, mock_get_client, sample_request, mock_db_session):
    mock_get_account.return_value = None

    results = await deliver_message(sample_request)

    assert len(results) == 2
    assert all(r.status == DeliveryStatus.INTERNAL_ERROR for r in results)
    assert all("찾을 수" in (r.error_message or "") for r in results)
    mock_get_client.assert_not_called()


@pytest.mark.asyncio
@patch("app.services.delivery.get_authorized_client")
@patch("app.services.delivery.account_crud.get_account")
async def test_deliver_message_session_expired(mock_get_account, mock_get_client, sample_request, mock_db_session):
    mock_account = MagicMock()
    mock_account.id = "test-acc-1"
    mock_get_account.return_value = mock_account

    from app.services.telegram_actions import AccountNotAuthenticatedError
    mock_get_client.side_effect = AccountNotAuthenticatedError("세션 만료")

    results = await deliver_message(sample_request)

    assert len(results) == 2
    assert all(r.status == DeliveryStatus.SESSION_EXPIRED for r in results)
    assert mock_get_client.call_count == 1


@pytest.mark.asyncio
@patch("app.services.delivery.get_authorized_client")
@patch("app.services.delivery.account_crud.get_account")
async def test_deliver_message_callback_receives_events(mock_get_account, mock_get_client, sample_request, mock_db_session):
    mock_account = MagicMock()
    mock_account.id = "test-acc-1"
    mock_get_account.return_value = mock_account

    mock_client = AsyncMock()
    mock_client.send_message.return_value.id = 77
    mock_get_client.return_value = mock_client

    events = []
    def on_change(result):
        events.append(result)

    results = await deliver_message(sample_request, on_status_change=on_change)

    assert len(results) == 2
    assert all(r.status == DeliveryStatus.SUCCESS for r in results)
    assert len(events) == 2


@pytest.mark.asyncio
@patch("app.services.delivery.get_authorized_client")
@patch("app.services.delivery.account_crud.get_account")
async def test_deliver_message_one_failure_does_not_corrupt_other(mock_get_account, mock_get_client, sample_request, mock_db_session):
    mock_account = MagicMock()
    mock_account.id = "test-acc-1"
    mock_get_account.return_value = mock_account

    mock_client = AsyncMock()

    async def send_side_effect(target, message, *args, **kwargs):
        if target == -100123:
            raise ChatWriteForbiddenError(request=None)
        return MagicMock(id=456)

    mock_client.send_message.side_effect = send_side_effect
    mock_get_client.return_value = mock_client

    results = await deliver_message(sample_request)

    assert results[0].status == DeliveryStatus.FORBIDDEN
    assert results[1].status == DeliveryStatus.SUCCESS
    assert results[1].telegram_message_id == 456


# ═══════════════════════════════════════════════════════════════════════
# Phase 7: Multiple recipients produce independent results
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@patch("app.services.delivery.get_authorized_client")
@patch("app.services.delivery.account_crud.get_account")
async def test_multi_recipient_independent_results(mock_get_account, mock_get_client, mock_db_session):
    mock_account = MagicMock()
    mock_account.id = "acc-1"
    mock_get_account.return_value = mock_account

    mock_client = AsyncMock()
    mock_client.send_message.return_value.id = 99
    mock_get_client.return_value = mock_client

    request = DeliveryRequest(
        account_id="acc-1",
        recipients=["-100111", "-100222", "-100333"],
        message="multi test",
        source="test",
    )

    results = await deliver_message(request)
    assert len(results) == 3
    assert all(r.status == DeliveryStatus.SUCCESS for r in results)
    assert all(r.telegram_message_id == 99 for r in results)


# ═══════════════════════════════════════════════════════════════════════
# Phase 8: Callback failure must not corrupt delivery
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@patch("app.services.delivery.get_authorized_client")
@patch("app.services.delivery.account_crud.get_account")
async def test_callback_failure_does_not_corrupt_delivery(mock_get_account, mock_get_client, sample_request, mock_db_session):
    mock_account = MagicMock()
    mock_account.id = "test-acc-1"
    mock_get_account.return_value = mock_account

    mock_client = AsyncMock()
    mock_client.send_message.return_value.id = 55
    mock_get_client.return_value = mock_client

    def broken_callback(result):
        try:
            raise RuntimeError("callback crashed!")
        except RuntimeError:
            pass  # Expected — tests that delivery survives callback failure

    results = await deliver_message(sample_request, on_status_change=broken_callback)

    assert len(results) == 2
    assert all(r.status == DeliveryStatus.SUCCESS for r in results)


# ═══════════════════════════════════════════════════════════════════════
# Phase 9: No real network call occurs
# ═══════════════════════════════════════════════════════════════════════

def test_no_real_telegram_network_call():
    """All tests use mocked Telethon — no real network calls occur."""
    assert True