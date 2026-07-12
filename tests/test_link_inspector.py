from unittest.mock import AsyncMock

import pytest
from telethon.errors import ChannelPrivateError, FloodWaitError, UsernameNotOccupiedError
from telethon.tl.types import Channel, Chat, ChatInvite, ChatInviteAlready

from app.schemas.link_inspector import LinkJoinTarget
from app.services.link_inspector_service import (
    DailyJoinLimitExceededError,
    inspect_links,
    join_selected_links,
    parse_telegram_link,
)


async def _create_account(client, phone="+821099990000"):
    res = await client.post("/api/accounts", json={"phone": phone})
    assert res.status_code == 201
    return res.json()["id"]


def _fake_channel(id_=100, title="테스트 채널", username="testchan", megagroup=False, participants_count=42):
    return Channel(
        id=id_,
        title=title,
        username=username,
        megagroup=megagroup,
        photo=None,
        date=None,
        participants_count=participants_count,
        access_hash=1,
    )


def _fake_chat(id_=200, title="테스트 그룹", participants_count=10):
    return Chat(
        id=id_,
        title=title,
        photo=None,
        participants_count=participants_count,
        date=None,
        version=0,
    )


# ─── parse_telegram_link ───────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("https://t.me/somechannel", ("username", "somechannel")),
    ("t.me/somechannel", ("username", "somechannel")),
    ("http://t.me/somechannel/123", ("username", "somechannel")),
    ("@somechannel", ("username", "somechannel")),
    ("somechannel", ("username", "somechannel")),
    ("https://t.me/+AbCdEf12345", ("invite", "AbCdEf12345")),
    ("https://t.me/joinchat/AbCdEf12345", ("invite", "AbCdEf12345")),
    ("https://t.me/c/123456/789", ("invalid", "https://t.me/c/123456/789")),
    ("", ("invalid", "")),
    ("   ", ("invalid", "   ")),
    ("@", ("invalid", "@")),
])
def test_parse_telegram_link(raw, expected):
    assert parse_telegram_link(raw) == expected


# ─── inspect_links (service layer, mocked Telethon client) ────────────


@pytest.mark.asyncio
async def test_inspect_active_username_channel(monkeypatch):
    fake_client = AsyncMock()
    fake_client.get_entity = AsyncMock(return_value=_fake_channel())
    monkeypatch.setattr(
        "app.services.link_inspector_service.get_authorized_client",
        AsyncMock(return_value=fake_client),
    )

    items, dupes = await inspect_links(account=object(), links=["https://t.me/testchan"])
    assert dupes == 0
    assert len(items) == 1
    item = items[0]
    assert item["status"] == "active"
    assert item["accessible"] is True
    assert item["chat_type"] == "channel"
    assert item["username"] == "testchan"
    assert item["participants_count"] == 42


@pytest.mark.asyncio
async def test_inspect_dead_username(monkeypatch):
    fake_client = AsyncMock()
    fake_client.get_entity = AsyncMock(side_effect=UsernameNotOccupiedError(request=None))
    monkeypatch.setattr(
        "app.services.link_inspector_service.get_authorized_client",
        AsyncMock(return_value=fake_client),
    )

    items, _ = await inspect_links(account=object(), links=["https://t.me/doesnotexist"])
    assert items[0]["status"] == "dead"
    assert items[0]["accessible"] is False


@pytest.mark.asyncio
async def test_inspect_private_channel(monkeypatch):
    fake_client = AsyncMock()
    fake_client.get_entity = AsyncMock(side_effect=ChannelPrivateError(request=None))
    monkeypatch.setattr(
        "app.services.link_inspector_service.get_authorized_client",
        AsyncMock(return_value=fake_client),
    )

    items, _ = await inspect_links(account=object(), links=["https://t.me/privatechan"])
    assert items[0]["status"] == "private"
    assert items[0]["accessible"] is False


@pytest.mark.asyncio
async def test_inspect_flood_wait(monkeypatch):
    fake_client = AsyncMock()
    fake_client.get_entity = AsyncMock(side_effect=FloodWaitError(request=None, capture=30))
    monkeypatch.setattr(
        "app.services.link_inspector_service.get_authorized_client",
        AsyncMock(return_value=fake_client),
    )

    items, _ = await inspect_links(account=object(), links=["https://t.me/somechan"])
    assert items[0]["status"] == "flood_wait"
    assert items[0]["accessible"] is False


@pytest.mark.asyncio
async def test_inspect_invalid_link_never_calls_telethon(monkeypatch):
    fake_client = AsyncMock()
    monkeypatch.setattr(
        "app.services.link_inspector_service.get_authorized_client",
        AsyncMock(return_value=fake_client),
    )

    items, _ = await inspect_links(account=object(), links=["not a valid link!!"])
    assert items[0]["status"] == "dead"
    fake_client.get_entity.assert_not_called()


@pytest.mark.asyncio
async def test_inspect_invite_already_joined(monkeypatch):
    fake_client = AsyncMock()
    fake_client.return_value = ChatInviteAlready(chat=_fake_chat())
    monkeypatch.setattr(
        "app.services.link_inspector_service.get_authorized_client",
        AsyncMock(return_value=fake_client),
    )

    items, _ = await inspect_links(account=object(), links=["https://t.me/+AbCdEf12345"])
    assert items[0]["status"] == "active"
    assert items[0]["chat_type"] == "group"
    assert items[0]["title"] == "테스트 그룹"


@pytest.mark.asyncio
async def test_inspect_invite_requires_approval(monkeypatch):
    fake_client = AsyncMock()
    fake_client.return_value = ChatInvite(
        title="승인 필요 그룹",
        photo=None,
        participants_count=5,
        color=0,
        megagroup=True,
        request_needed=True,
    )
    monkeypatch.setattr(
        "app.services.link_inspector_service.get_authorized_client",
        AsyncMock(return_value=fake_client),
    )

    items, _ = await inspect_links(account=object(), links=["https://t.me/+NeedsApproval"])
    assert items[0]["status"] == "private"
    assert items[0]["accessible"] is False


@pytest.mark.asyncio
async def test_inspect_dedupes_before_calling_telethon(monkeypatch):
    fake_client = AsyncMock()
    fake_client.get_entity = AsyncMock(return_value=_fake_channel())
    monkeypatch.setattr(
        "app.services.link_inspector_service.get_authorized_client",
        AsyncMock(return_value=fake_client),
    )

    links = ["https://t.me/testchan", "@testchan", "t.me/testchan?x=1", "TESTCHAN"]
    items, dupes = await inspect_links(account=object(), links=links)
    assert dupes == 3
    assert len(items) == 1
    assert fake_client.get_entity.call_count == 1


# ─── join_selected_links ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_join_selected_links_respects_daily_limit(monkeypatch):
    from app.models.account import Account

    # get_authorized_client is called before the daily-limit check (matches
    # group_search_service.join_selected_groups' ordering), so it needs mocking
    # even though this test only cares about the limit short-circuit below it.
    monkeypatch.setattr(
        "app.services.link_inspector_service.get_authorized_client",
        AsyncMock(return_value=AsyncMock()),
    )
    monkeypatch.setattr(
        "app.services.link_inspector_service.group_search_crud.count_today_joins",
        AsyncMock(return_value=5),  # MAX_DAILY_JOINS
    )
    account = Account(id="acc-1", tenant_id="t-1", phone="+821000000000")

    with pytest.raises(DailyJoinLimitExceededError):
        await join_selected_links(account, [LinkJoinTarget(raw_link="@foo", title="Foo")])


@pytest.mark.asyncio
async def test_join_selected_links_success(monkeypatch):
    from app.models.account import Account

    fake_client = AsyncMock()
    fake_client.get_entity = AsyncMock(return_value=_fake_channel())
    monkeypatch.setattr(
        "app.services.link_inspector_service.get_authorized_client",
        AsyncMock(return_value=fake_client),
    )
    monkeypatch.setattr(
        "app.services.link_inspector_service.group_search_crud.count_today_joins",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "app.services.link_inspector_service.group_search_crud.create_join_log",
        AsyncMock(),
    )
    account = Account(id="acc-1", tenant_id="t-1", phone="+821000000000")

    results = await join_selected_links(account, [LinkJoinTarget(raw_link="@testchan", title="테스트 채널")])
    assert results[0]["success"] is True
    assert results[0]["chat_id"] == str(_fake_channel().id)


@pytest.mark.asyncio
async def test_join_selected_links_via_invite_hash(monkeypatch):
    from unittest.mock import MagicMock

    from app.models.account import Account

    fake_client = AsyncMock()
    updates = MagicMock()
    updates.chats = [_fake_chat()]
    fake_client.return_value = updates  # client(ImportChatInviteRequest(...))
    monkeypatch.setattr(
        "app.services.link_inspector_service.get_authorized_client",
        AsyncMock(return_value=fake_client),
    )
    monkeypatch.setattr(
        "app.services.link_inspector_service.group_search_crud.count_today_joins",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "app.services.link_inspector_service.group_search_crud.create_join_log",
        AsyncMock(),
    )
    account = Account(id="acc-1", tenant_id="t-1", phone="+821000000000")

    results = await join_selected_links(account, [LinkJoinTarget(raw_link="https://t.me/+AbCdEf12345", title="초대 그룹")])
    assert results[0]["success"] is True
    assert results[0]["chat_id"] == str(_fake_chat().id)


@pytest.mark.asyncio
async def test_join_selected_links_invalid_link_reported_as_failure(monkeypatch):
    from app.models.account import Account

    monkeypatch.setattr(
        "app.services.link_inspector_service.get_authorized_client",
        AsyncMock(return_value=AsyncMock()),
    )
    monkeypatch.setattr(
        "app.services.link_inspector_service.group_search_crud.count_today_joins",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "app.services.link_inspector_service.group_search_crud.create_join_log",
        AsyncMock(),
    )
    account = Account(id="acc-1", tenant_id="t-1", phone="+821000000000")

    results = await join_selected_links(account, [LinkJoinTarget(raw_link="not a valid link!!", title="Bad")])
    assert results[0]["success"] is False


# ─── API routes ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inspect_route_unknown_account_returns_404(client):
    res = await client.post("/api/link-inspector/inspect", json={"account_id": "nope", "links": ["@foo"]})
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_inspect_route_success(client, monkeypatch):
    account_id = await _create_account(client)
    monkeypatch.setattr(
        "app.api.link_inspector.inspect_links",
        AsyncMock(return_value=([{
            "raw_link": "@foo",
            "status": "active",
            "accessible": True,
            "title": "Foo",
            "chat_type": "channel",
            "username": "foo",
            "chat_id": "1",
            "participants_count": 10,
        }], 2)),
    )
    res = await client.post(
        "/api/link-inspector/inspect",
        json={"account_id": account_id, "links": ["@foo", "@foo", "@bar"]},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["total_submitted"] == 3
    assert body["duplicates_removed"] == 2
    assert body["total_inspected"] == 1
    assert body["items"][0]["username"] == "foo"


@pytest.mark.asyncio
async def test_inspect_route_session_expired_returns_400(client, monkeypatch):
    from app.services.telegram_actions import AccountNotAuthenticatedError

    account_id = await _create_account(client)
    monkeypatch.setattr(
        "app.api.link_inspector.inspect_links",
        AsyncMock(side_effect=AccountNotAuthenticatedError("텔레그램 세션이 만료되었습니다. 다시 인증해주세요.")),
    )
    res = await client.post("/api/link-inspector/inspect", json={"account_id": account_id, "links": ["@foo"]})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_join_route_daily_limit_returns_429(client, monkeypatch):
    account_id = await _create_account(client)
    monkeypatch.setattr(
        "app.api.link_inspector.join_selected_links",
        AsyncMock(side_effect=DailyJoinLimitExceededError("일일 가입 한도 초과 (최대 5회)")),
    )
    res = await client.post(
        "/api/link-inspector/join",
        json={"account_id": account_id, "targets": [{"raw_link": "@foo", "title": "Foo"}]},
    )
    assert res.status_code == 429


@pytest.mark.asyncio
async def test_join_route_success(client, monkeypatch):
    account_id = await _create_account(client)
    monkeypatch.setattr(
        "app.api.link_inspector.join_selected_links",
        AsyncMock(return_value=[{"chat_id": "1", "title": "Foo", "success": True, "error": None}]),
    )
    res = await client.post(
        "/api/link-inspector/join",
        json={"account_id": account_id, "targets": [{"raw_link": "@foo", "title": "Foo"}]},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["items"][0]["success"] is True
