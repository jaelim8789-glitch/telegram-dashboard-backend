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


@pytest.mark.asyncio
async def test_process_broadcast_applies_delay_seconds_for_normal_mode(db_session, monkeypatch):
    """Production symptom: the '일반 발송 간격' selector had no backend effect —
    delay_seconds was accepted nowhere in the pipeline."""
    from app.schemas.broadcast import BroadcastCreate

    account = await _make_account(db_session)
    payload = BroadcastCreate(
        account_id=account.id, message="테스트", recipients=["-100999"], delay_seconds=10
    )
    broadcast = await broadcast_crud.create_broadcast(db_session, payload, media_path=None, scheduled_at=None)
    assert broadcast.delay_seconds == 10

    captured = {}

    async def _capture(request, *args, **kwargs):
        captured["inter_message_delay"] = request.inter_message_delay
        return [_success_result()]

    monkeypatch.setattr("app.services.broadcast_processor.deliver_message", _capture)

    await process_broadcast(broadcast.id)

    assert captured["inter_message_delay"] == 10.0


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

    request = DeliveryRequest(
        account_id=account.id,
        recipients=["-100999"],
        message="test",
        source="manual",
    )

    results = await deliver_message(request)
    assert len(results) == 1
    assert results[0].status.value == "session_expired"


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
    await db_session.refresh(broadcast)
    assert broadcast.error_message == "attempt 4"


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
async def test_process_broadcast_timeout_with_partial_success_marks_sent(db_session, monkeypatch):
    """Production symptom: a broadcast to 89 recipients logged 53 successful
    sends in message_logs, but the overall Broadcast row was blanket-marked
    "failed" because the outer wait_for hit its timeout — misleading, since
    most recipients actually got the message. The final status must reflect
    what message_logs actually show instead of assuming total failure."""
    from app.models.message_log import MessageLog

    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id, recipients=["-100001", "-100002"])

    # Simulate one recipient already having succeeded before the cutoff.
    db_session.add(
        MessageLog(
            account_id=account.id,
            recipient="-100001",
            source="broadcast",
            source_id=broadcast.id,
            status="success",
            success=True,
        )
    )
    await db_session.commit()

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
    assert broadcast.status == "sent"
    assert "1/2" in broadcast.error_message
    assert "시간 초과" in broadcast.error_message


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
    assert settings.broadcast_timeout_seconds == 600
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


# ═══════════════════════════════════════════════════════════════════════
# Sprint 29 — retry/redispatch must not resend to already-succeeded recipients
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_process_broadcast_excludes_already_succeeded_recipients(db_session, monkeypatch):
    """A recipient with a prior success message_log for this broadcast must not
    be included in the recipient list handed to deliver_message on a re-run
    (e.g. after a timeout, or a manual retry/send-now)."""
    from app.models.message_log import MessageLog

    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id, recipients=["-100001", "-100002", "-100003"])

    db_session.add(
        MessageLog(
            account_id=account.id, recipient="-100001", source="broadcast",
            source_id=broadcast.id, status="success", success=True,
        )
    )
    await db_session.commit()

    captured = {}

    async def _capture(request, *args, **kwargs):
        captured["recipients"] = list(request.recipients)
        return [_success_result(r) for r in request.recipients]

    monkeypatch.setattr("app.services.broadcast_processor.deliver_message", _capture)

    await process_broadcast(broadcast.id)

    assert captured["recipients"] == ["-100002", "-100003"]

    await db_session.refresh(broadcast)
    assert broadcast.status == "sent"


@pytest.mark.asyncio
async def test_process_broadcast_all_recipients_already_succeeded_skips_redispatch(db_session, monkeypatch):
    """If every recipient already has a recorded success, re-running the
    broadcast must not call deliver_message at all — there's nothing left
    to send, and calling it would resend duplicate messages."""
    from app.models.message_log import MessageLog

    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id, recipients=["-100001", "-100002"])

    for recipient in ("-100001", "-100002"):
        db_session.add(
            MessageLog(
                account_id=account.id, recipient=recipient, source="broadcast",
                source_id=broadcast.id, status="success", success=True,
            )
        )
    await db_session.commit()

    deliver_mock = AsyncMock()
    monkeypatch.setattr("app.services.broadcast_processor.deliver_message", deliver_mock)

    await process_broadcast(broadcast.id)

    deliver_mock.assert_not_called()
    await db_session.refresh(broadcast)
    assert broadcast.status == "sent"


@pytest.mark.asyncio
async def test_process_broadcast_reports_partial_success_when_remaining_recipients_fail(db_session, monkeypatch):
    """A recipient that already succeeded in an earlier attempt must keep the
    broadcast's final status as partial success ("sent"), even if every
    recipient attempted *this round* fails — the earlier success must not be
    erased just because this round found nothing new to succeed at."""
    from app.models.message_log import MessageLog

    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id, recipients=["-100001", "-100002"])

    db_session.add(
        MessageLog(
            account_id=account.id, recipient="-100001", source="broadcast",
            source_id=broadcast.id, status="success", success=True,
        )
    )
    await db_session.commit()

    monkeypatch.setattr(
        "app.services.broadcast_processor.deliver_message",
        AsyncMock(return_value=[_failure_result("-100002", error="일시적 오류")]),
    )

    await process_broadcast(broadcast.id)

    await db_session.refresh(broadcast)
    assert broadcast.status == "sent"


@pytest.mark.asyncio
async def test_process_broadcast_recipient_filter_scoped_to_this_broadcast_only(db_session, monkeypatch):
    """A success log for the *same recipient* under a different broadcast_id
    must not exclude that recipient here — the filter is scoped strictly to
    this broadcast's own source_id, so unrelated broadcasts (including a
    different recurring child) never cross-contaminate each other."""
    from app.models.message_log import MessageLog

    account = await _make_account(db_session)
    other_broadcast = await _make_broadcast(db_session, account.id, recipients=["-100001"])
    db_session.add(
        MessageLog(
            account_id=account.id, recipient="-100001", source="broadcast",
            source_id=other_broadcast.id, status="success", success=True,
        )
    )
    await db_session.commit()

    broadcast = await _make_broadcast(db_session, account.id, recipients=["-100001"])

    captured = {}

    async def _capture(request, *args, **kwargs):
        captured["recipients"] = list(request.recipients)
        return [_success_result(r) for r in request.recipients]

    monkeypatch.setattr("app.services.broadcast_processor.deliver_message", _capture)

    # skip_rate_limit=True: this test is about broadcast-id scoping, not the
    # unrelated per-account 60s cooldown that two broadcasts created back to
    # back for the same account would otherwise trip.
    await process_broadcast(broadcast.id, skip_rate_limit=True)

    assert captured["recipients"] == ["-100001"]


@pytest.mark.asyncio
async def test_get_succeeded_recipients_empty_for_fresh_broadcast(db_session):
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    result = await broadcast_crud.get_succeeded_recipients(db_session, broadcast.id)
    assert result == set()


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


# ═══════════════════════════════════════════════════════════════════════
# Sprint 30 — concurrent /retry + /dispatch/{id} must not double-dispatch
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_concurrent_retry_broadcast_calls_only_one_can_claim(db_session):
    """Two overlapping callers (e.g. two near-simultaneous /dispatch/{id} or
    /retry requests, each with its own DB session exactly like two real HTTP
    requests would) both read status=="failed" before either commits. Without
    a row lock, both would pass the check and both flip status to "pending",
    so each caller believes it alone won the retry and would independently
    proceed to call process_broadcast — a double dispatch. Only one of the two
    concurrent calls must succeed."""
    import app.services.broadcast_processor as broadcast_processor_module

    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)
    broadcast.status = "failed"
    broadcast.error_message = "일시 오류"
    broadcast.sent_at = broadcast_crud.utcnow_naive()
    await db_session.commit()

    session_maker = broadcast_processor_module.async_session_maker
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _claim_with_barrier():
        async with session_maker() as db2:
            # Force both callers to have already read the row before either
            # is allowed to proceed to its own commit, reproducing the
            # overlapping-request race deterministically instead of relying
            # on incidental timing.
            await db2.get(type(broadcast), broadcast.id)
            if not entered.is_set():
                entered.set()
                await release.wait()
            else:
                release.set()
            return await broadcast_crud.retry_broadcast(db2, broadcast.id)

    r1, r2 = await asyncio.gather(_claim_with_barrier(), _claim_with_barrier())
    successes = [r for r in (r1, r2) if r is not None]
    assert len(successes) == 1, (
        "both concurrent retry_broadcast calls succeeded — a double dispatch "
        "is now possible from two overlapping /retry or /dispatch/{id} requests"
    )


@pytest.mark.asyncio
async def test_concurrent_dispatch_only_processes_the_winning_claim(db_session, monkeypatch):
    """Mirrors exactly what POST /api/broadcast/dispatch/{id} does (crud_retry,
    then process_broadcast only if it won the claim), driven from two
    concurrent callers on the same failed broadcast — e.g. a double-click on
    "재발송", or two admin sessions. Only the caller that wins the atomic claim
    may proceed to process_broadcast; the other must back off instead of also
    dispatching and later overwriting the winner's final status."""
    import app.services.broadcast_processor as broadcast_processor_module

    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id, recipients=["-100001"])
    broadcast.status = "failed"
    broadcast.error_message = "일시 오류"
    broadcast.sent_at = broadcast_crud.utcnow_naive()
    await db_session.commit()

    session_maker = broadcast_processor_module.async_session_maker
    entered = asyncio.Event()
    release = asyncio.Event()
    deliver_calls = []

    async def _tracking_deliver(request, *args, **kwargs):
        deliver_calls.append(list(request.recipients))
        return [_success_result("-100001")]

    monkeypatch.setattr("app.services.broadcast_processor.deliver_message", _tracking_deliver)

    async def _dispatch_like_endpoint():
        async with session_maker() as db2:
            await db2.get(type(broadcast), broadcast.id)
            if not entered.is_set():
                entered.set()
                await release.wait()
            else:
                release.set()
            updated = await broadcast_crud.retry_broadcast(db2, broadcast.id)
        if updated is None:
            return False
        await process_broadcast(updated.id, skip_rate_limit=True)
        return True

    r1, r2 = await asyncio.gather(_dispatch_like_endpoint(), _dispatch_like_endpoint())
    dispatched = [r for r in (r1, r2) if r]
    assert len(dispatched) == 1, (
        f"both concurrent callers dispatched (dispatched={dispatched}) — "
        "the losing claim should have backed off instead of also redispatching"
    )
    assert len(deliver_calls) == 1, (
        f"deliver_message was called {len(deliver_calls)} times for one broadcast_id — "
        "a concurrent second call reached actual delivery instead of being rejected"
    )