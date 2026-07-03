from unittest.mock import AsyncMock

import pytest

from app.crud import account as account_crud
from app.crud import broadcast as broadcast_crud
from app.schemas.account import AccountCreate
from app.schemas.broadcast import BroadcastCreate
from app.services.broadcast_processor import process_broadcast


async def _make_account(db_session, phone="+821011119999"):
    return await account_crud.create_account(db_session, AccountCreate(phone=phone))


async def _make_broadcast(db_session, account_id, message="테스트 발송", recipients=None):
    payload = BroadcastCreate(account_id=account_id, message=message, recipients=recipients or ["-100999"])
    return await broadcast_crud.create_broadcast(db_session, payload, media_path=None, scheduled_at=None)


@pytest.mark.asyncio
async def test_process_broadcast_success(db_session, monkeypatch):
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    monkeypatch.setattr(
        "app.services.broadcast_processor.run_broadcast", AsyncMock(return_value=(True, None))
    )

    await process_broadcast(broadcast.id)

    # process_broadcast() updates the row through its own separate session, so this
    # session's identity map still holds the pre-update object — refresh() reloads it in
    # place via a proper async round-trip (plain attribute access after expire_all()
    # would try to lazy-load synchronously and blow up with MissingGreenlet).
    await db_session.refresh(broadcast)
    assert broadcast.status == "sent"
    assert broadcast.sent_at is not None
    assert broadcast.error_message is None


@pytest.mark.asyncio
async def test_process_broadcast_failure_records_error(db_session, monkeypatch):
    account = await _make_account(db_session)
    broadcast = await _make_broadcast(db_session, account.id)

    monkeypatch.setattr(
        "app.services.broadcast_processor.run_broadcast",
        AsyncMock(return_value=(False, "계정이 아직 인증되지 않았습니다.")),
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
    run_broadcast_mock = AsyncMock()
    monkeypatch.setattr("app.services.broadcast_processor.run_broadcast", run_broadcast_mock)

    await process_broadcast(broadcast.id)

    await db_session.refresh(broadcast)
    assert broadcast.status == "failed"
    assert "계정을 찾을 수 없습니다" in broadcast.error_message
    run_broadcast_mock.assert_not_called()


@pytest.mark.asyncio
async def test_process_broadcast_rate_limited_marks_failed_without_sending(db_session, monkeypatch):
    account = await _make_account(db_session)
    first = await _make_broadcast(db_session, account.id)

    run_broadcast_mock = AsyncMock(return_value=(True, None))
    monkeypatch.setattr("app.services.broadcast_processor.run_broadcast", run_broadcast_mock)

    # Process the first one to completion so it actually records a sent_at close to "now"
    # — created *and finished* before the second one even exists, matching the real
    # back-to-back-immediate-sends scenario this guards against.
    await process_broadcast(first.id)
    run_broadcast_mock.reset_mock()

    second = await _make_broadcast(db_session, account.id, message="두번째")
    await process_broadcast(second.id)

    # Still within the 1/min cooldown from the first send -> the real Telegram-calling
    # path must not run, and — with no queue to push a retry into — this is reported as
    # a clean failure rather than silently retried.
    run_broadcast_mock.assert_not_called()

    await db_session.refresh(second)
    assert second.status == "failed"
    assert "1분에 1회" in second.error_message
