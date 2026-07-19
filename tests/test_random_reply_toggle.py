"""Regression coverage for the simplified random-reply on/off toggle — the
one feature that got the most user-facing confusion this session (turned out
to be a deploy/cache gap, not a backend bug, but it had zero test coverage
proving that end to end). Covers:

1. GET/PUT /toggle — the actual API the frontend calls.
2. list_active_with_message — what the scheduler polls.
3. execute_random_reply resolving target_chats=[] to "every group the
   account is currently in" (the whole point of the simplified toggle —
   no manually picked target list).
"""

import itertools
from unittest.mock import AsyncMock, MagicMock

import pytest

_phone_seq = itertools.count(1)


async def _create_account(client, phone=None):
    phone = phone or f"+821099{next(_phone_seq):06d}"
    res = await client.post("/api/accounts", json={"phone": phone, "name": "랜덤답장 테스트 계정"})
    assert res.status_code == 201, res.text
    return res.json()["id"]


# ─── Toggle API ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_toggle_lazily_creates_inactive_macro(client):
    account_id = await _create_account(client)
    res = await client.get(f"/api/accounts/{account_id}/reply-macros/toggle")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["is_active"] is False
    assert body["message_content"] == ""


@pytest.mark.asyncio
async def test_put_toggle_on_with_message_persists(client):
    account_id = await _create_account(client)
    res = await client.put(
        f"/api/accounts/{account_id}/reply-macros/toggle",
        json={"is_active": True, "message_content": "안녕하세요! 문의 감사합니다."},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["is_active"] is True
    assert body["message_content"] == "안녕하세요! 문의 감사합니다."

    # Re-fetch confirms it actually persisted, not just echoed in the response.
    refetch = await client.get(f"/api/accounts/{account_id}/reply-macros/toggle")
    assert refetch.json() == {"is_active": True, "message_content": "안녕하세요! 문의 감사합니다."}


@pytest.mark.asyncio
async def test_put_toggle_on_without_any_message_is_422(client):
    account_id = await _create_account(client)
    res = await client.put(
        f"/api/accounts/{account_id}/reply-macros/toggle",
        json={"is_active": True},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_put_toggle_off_persists(client):
    account_id = await _create_account(client)
    await client.put(
        f"/api/accounts/{account_id}/reply-macros/toggle",
        json={"is_active": True, "message_content": "hi"},
    )
    res = await client.put(
        f"/api/accounts/{account_id}/reply-macros/toggle",
        json={"is_active": False},
    )
    assert res.status_code == 200
    assert res.json()["is_active"] is False


# ─── Scheduler polling query ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_active_with_message_excludes_inactive_and_empty_message(db_session):
    from app.crud import reply_macro as macro_crud

    on_with_msg = await macro_crud.get_or_create_for_account(db_session, "acct-on")
    on_with_msg.is_active = True
    on_with_msg.message_content = "안녕"
    off = await macro_crud.get_or_create_for_account(db_session, "acct-off")
    off.is_active = False
    off.message_content = "안녕"
    on_no_msg = await macro_crud.get_or_create_for_account(db_session, "acct-on-empty")
    on_no_msg.is_active = True
    on_no_msg.message_content = ""
    await db_session.commit()

    active = await macro_crud.list_active_with_message(db_session)
    account_ids = {m.account_id for m in active}
    assert account_ids == {"acct-on"}


# ─── execute_random_reply: target_chats=[] resolves to all dialogs ─────


class db_session_cm:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _fake_dialog(entity_id: int, is_group=True, is_channel=False):
    dialog = MagicMock()
    dialog.entity.id = entity_id
    dialog.is_group = is_group
    dialog.is_channel = is_channel
    return dialog


@pytest.mark.asyncio
async def test_execute_random_reply_resolves_empty_targets_to_all_account_groups(db_session, monkeypatch):
    """The simplified toggle never sets target_chats — this is the behavior
    that makes "그냥 켜면 모든 그룹에 나간다" true. If this regresses, the
    toggle silently does nothing (exactly last night's user-facing symptom)."""
    import app.services.random_reply_service as rrs
    from app.crud import account as account_crud
    from app.crud import reply_macro as macro_crud
    from app.models.account import Account
    from app.services.delivery import DeliveryResult, DeliveryStatus

    monkeypatch.setattr(rrs, "async_session_maker", lambda: db_session_cm(db_session))

    account = Account(id="acct-dialogs", phone="+821011112222", name="대상계정", status="active")
    db_session.add(account)
    await db_session.flush()

    macro = await macro_crud.get_or_create_for_account(db_session, "acct-dialogs")
    macro.is_active = True
    macro.message_content = "자동 답장 메시지"
    assert macro.target_chats == "[]"
    await db_session.commit()

    sender = MagicMock(id=555)
    incoming_msg = MagicMock(out=False, id=42, get_sender=AsyncMock(return_value=sender))

    fake_client = MagicMock()
    fake_client.is_connected.return_value = True
    fake_client.iter_dialogs = lambda: _async_iter([_fake_dialog(-1001, is_group=True)])
    fake_client.get_messages = AsyncMock(return_value=[incoming_msg])

    monkeypatch.setattr(rrs, "get_authorized_client", AsyncMock(return_value=fake_client))
    deliver_mock = AsyncMock(
        return_value=[DeliveryResult(status=DeliveryStatus.SUCCESS, recipient="-1001", telegram_message_id=999)]
    )
    monkeypatch.setattr(rrs, "deliver_message", deliver_mock)

    result = await rrs.execute_random_reply(macro.id)

    assert result["status"] == "completed" or "results" in result, result
    deliver_mock.assert_awaited_once()
    sent_request = deliver_mock.await_args.kwargs.get("request") or deliver_mock.await_args.args[0]
    assert sent_request.recipients == ["-1001"]
    assert sent_request.message == "자동 답장 메시지"


async def _async_iter(items):
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_execute_random_reply_skips_inactive_macro(db_session, monkeypatch):
    import app.services.random_reply_service as rrs
    from app.crud import reply_macro as macro_crud

    monkeypatch.setattr(rrs, "async_session_maker", lambda: db_session_cm(db_session))

    macro = await macro_crud.get_or_create_for_account(db_session, "acct-inactive")
    macro.is_active = False
    await db_session.commit()

    result = await rrs.execute_random_reply(macro.id)
    assert result == {"status": "skipped", "reason": "not_found_or_inactive"}
