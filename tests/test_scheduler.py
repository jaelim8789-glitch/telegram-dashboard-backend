from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.crud import account as account_crud
from app.crud import broadcast as broadcast_crud
from app.schemas.account import AccountCreate
from app.schemas.broadcast import BroadcastCreate
from app.scheduler.scheduler import dispatch_due_broadcasts


async def _make_account(db_session, phone="+821022229999"):
    return await account_crud.create_account(db_session, AccountCreate(phone=phone))


async def _make_scheduled_broadcast(db_session, account_id, *, seconds_ago=5, message="예약 발송"):
    payload = BroadcastCreate(account_id=account_id, message=message, recipients=["-100999"])
    scheduled_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=seconds_ago)
    return await broadcast_crud.create_broadcast(db_session, payload, media_path=None, scheduled_at=scheduled_at)


@pytest.mark.asyncio
async def test_dispatch_processes_due_broadcast(db_session, monkeypatch):
    account = await _make_account(db_session)
    broadcast = await _make_scheduled_broadcast(db_session, account.id)

    monkeypatch.setattr("app.scheduler.scheduler.process_broadcast", AsyncMock(return_value=None))
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
