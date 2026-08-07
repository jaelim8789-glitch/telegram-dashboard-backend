from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.crud import account as account_crud
from app.crud import auto_reply as auto_reply_crud
from app.models.auto_reply import AutoReplyLog, AutoReplyRule
from app.schemas.account import AccountCreate
from app.schemas.auto_reply import AutoReplyRuleCreate
from app.services.auto_reply_service import _handle_incoming_message, _matches
import app.services.ai_reply_service as ai_reply_service_module


def _fake_event(text: str, *, out: bool = False, sender_id: int = 111, chat_id: int = 222, username="tester", message_id: int = 333):
    sender = SimpleNamespace(username=username, first_name="Tester")
    message = SimpleNamespace(id=message_id)
    return SimpleNamespace(
        out=out,
        raw_text=text,
        sender_id=sender_id,
        chat_id=chat_id,
        message=message,
        get_sender=AsyncMock(return_value=sender),
        reply=AsyncMock(),
    )


async def _make_account(db_session, *, auto_reply_enabled=True, phone="+821022223333"):
    account = await account_crud.create_account(db_session, AccountCreate(phone=phone))
    if auto_reply_enabled:
        account = await account_crud.set_auto_reply_enabled(db_session, account, True)
    return account


async def _make_rule(db_session, account_id, **overrides):
    payload = AutoReplyRuleCreate(
        name=overrides.pop("name", " "),
        match_type=overrides.pop("match_type", "keyword"),
        match_value=overrides.pop("match_value", "키워드"),
        reply_content=overrides.pop("reply_content", " 10,000"),
        cooldown_hours=overrides.pop("cooldown_hours", 1),
        max_replies_per_day=overrides.pop("max_replies_per_day", 100),
    )
    return await auto_reply_crud.create_rule(db_session, account_id, payload)


async def _seed_log(db_session, rule_id, account_id, *, user_id="111", status="success", created_at=None):
    from datetime import timedelta

    log = AutoReplyLog(
        rule_id=rule_id,
        account_id=account_id,
        chat_id="222",
        user_id=user_id,
        user_name="tester",
        trigger_message=" ",
        reply_sent=" 10,000",
        status=status,
    )
    db_session.add(log)
    await db_session.commit()
    await db_session.refresh(log)
    if created_at is not None:
        log.created_at = created_at
        await db_session.commit()
    return log


def test_matches_keyword_is_case_insensitive_substring():
    rule = SimpleNamespace(match_type="keyword", match_value="Price")
    assert _matches(rule, "what's the price")
    assert not _matches(rule, "hello there")


def test_matches_exact_requires_full_match_after_strip():
    rule = SimpleNamespace(match_type="exact", match_value="테스트")
    assert _matches(rule, " 테스트 ")
    assert not _matches(rule, "테스트 이어짐")


@pytest.mark.asyncio
async def test_handle_incoming_message_sends_reply_and_logs_success(db_session, monkeypatch):
    account = await _make_account(db_session)
    rule = await _make_rule(db_session, account.id)
    event = _fake_event("키워드")

    fake_client = AsyncMock()
    monkeypatch.setattr("app.services.auto_reply_service.get_authorized_client", AsyncMock(return_value=fake_client))
    fake_deliver = AsyncMock(return_value=[MagicMock(status="success")])
    monkeypatch.setattr("app.services.auto_reply_service.deliver_message", fake_deliver)

    await _handle_incoming_message(event, account.id)

    fake_deliver.assert_awaited_once()
    logs = await auto_reply_crud.list_logs(db_session, account.id)
    assert len(logs) == 1
    assert logs[0].status == "success"
    assert logs[0].reply_sent == rule.reply_content


@pytest.mark.asyncio
async def test_handle_incoming_message_ignores_own_outgoing_messages(db_session):
    account = await _make_account(db_session)
    await _make_rule(db_session, account.id)
    event = _fake_event(" ", out=True)

    await _handle_incoming_message(event, account.id)

    event.reply.assert_not_called()
    assert await auto_reply_crud.list_logs(db_session, account.id) == []


@pytest.mark.asyncio
async def test_handle_incoming_message_skips_when_master_switch_off(db_session):
    account = await _make_account(db_session, auto_reply_enabled=False)
    await _make_rule(db_session, account.id)
    event = _fake_event(" ")

    await _handle_incoming_message(event, account.id)

    event.reply.assert_not_called()
    assert await auto_reply_crud.list_logs(db_session, account.id) == []


@pytest.mark.asyncio
async def test_handle_incoming_message_no_keyword_match_does_nothing(db_session, monkeypatch):
    account = await _make_account(db_session)
    await _make_rule(db_session, account.id)
    event = _fake_event("")

    monkeypatch.setattr("app.services.auto_reply_service.get_authorized_client", AsyncMock(return_value=AsyncMock()))

    await _handle_incoming_message(event, account.id)

    event.reply.assert_not_called()
    assert await auto_reply_crud.list_logs(db_session, account.id) == []


@pytest.mark.asyncio
async def test_handle_incoming_message_cooldown_blocks_repeat_from_same_user(db_session, monkeypatch):
    account = await _make_account(db_session)
    rule = await _make_rule(db_session, account.id, cooldown_hours=1)
    await _seed_log(db_session, rule.id, account.id, user_id="111", status="success")

    event = _fake_event("키워드", sender_id=111)

    monkeypatch.setattr("app.services.auto_reply_service.get_authorized_client", AsyncMock(return_value=AsyncMock()))

    await _handle_incoming_message(event, account.id)

    event.reply.assert_not_called()
    logs = await auto_reply_crud.list_logs(db_session, account.id)
    assert len(logs) == 2
    rate_limited_logs = [log for log in logs if log.status == "rate_limited"]
    assert len(rate_limited_logs) == 1, f"Expected 1 rate_limited log, got {len(rate_limited_logs)}: {[(l.status, l.created_at) for l in logs]}"
    success_logs = [log for log in logs if log.status == "success"]
    assert len(success_logs) == 1


@pytest.mark.asyncio
async def test_handle_incoming_message_daily_limit_blocks_new_user_once_reached(db_session, monkeypatch):
    account = await _make_account(db_session)
    rule = await _make_rule(db_session, account.id, max_replies_per_day=1)
    await _seed_log(db_session, rule.id, account.id, user_id="999", status="success")

    event = _fake_event("키워드", sender_id=111)

    monkeypatch.setattr("app.services.auto_reply_service.get_authorized_client", AsyncMock(return_value=AsyncMock()))

    await _handle_incoming_message(event, account.id)

    event.reply.assert_not_called()
    logs = await auto_reply_crud.list_logs(db_session, account.id)
    rate_limited = [log for log in logs if log.status == "rate_limited"]
    assert len(rate_limited) == 1
    assert rate_limited[0].user_id == "111"


@pytest.mark.asyncio
async def test_handle_incoming_message_ai_fallback_off_by_default_no_deepseek_call(db_session, monkeypatch):
    """Preserves existing behavior: without opting in, a non-matching message
    still does nothing at all  no suggestion, no DeepSeek call."""
    account = await _make_account(db_session)
    await _make_rule(db_session, account.id)
    fake_ollama = AsyncMock(return_value="  ")
    monkeypatch.setattr(ai_reply_service_module, "_call_ollama", fake_ollama)

    event = _fake_event("질문")
    await _handle_incoming_message(event, account.id)

    event.reply.assert_not_called()
    fake_ollama.assert_not_called()
    from app.crud import auto_reply as _auto_reply_crud

    assert await _auto_reply_crud.list_suggestions(db_session, account.id) == []


@pytest.mark.asyncio
async def test_handle_incoming_message_ai_fallback_records_suggestion_when_enabled(db_session, monkeypatch):
    account = await _make_account(db_session)
    account.ai_fallback_reply_enabled = True
    await db_session.commit()
    await _make_rule(db_session, account.id)
    fake_ollama = AsyncMock(return_value="안녕하세요! 좋은 하루 되세요.")
    monkeypatch.setattr(ai_reply_service_module, "_call_ollama", fake_ollama)
    monkeypatch.setattr("app.services.auto_reply_service.get_authorized_client", AsyncMock(return_value=AsyncMock()))

    event = _fake_event("질문", sender_id=321, chat_id=654)
    await _handle_incoming_message(event, account.id)

    event.reply.assert_not_called()  # suggestion-only  never auto-sent
    fake_ollama.assert_awaited_once()
    suggestions = await auto_reply_crud.list_suggestions(db_session, account.id)
    assert len(suggestions) == 1
    assert suggestions[0].suggested_reply == "안녕하세요! 좋은 하루 되세요."
    assert suggestions[0].reviewed is False
    assert suggestions[0].chat_id == "654"
    assert suggestions[0].user_id == "321"


@pytest.mark.asyncio
async def test_handle_incoming_message_ai_fallback_deepseek_failure_records_nothing(db_session, monkeypatch):
    account = await _make_account(db_session)
    account.ai_fallback_reply_enabled = True
    await db_session.commit()
    await _make_rule(db_session, account.id)
    fake_ollama = AsyncMock(return_value=None)
    monkeypatch.setattr(ai_reply_service_module, "_call_ollama", fake_ollama)

    event = _fake_event("")
    await _handle_incoming_message(event, account.id)

    event.reply.assert_not_called()
    assert await auto_reply_crud.list_suggestions(db_session, account.id) == []


@pytest.mark.asyncio
async def test_handle_incoming_message_send_failure_logs_failed_status(db_session, monkeypatch):
    account = await _make_account(db_session)
    rule = await _make_rule(db_session, account.id)
    event = _fake_event("키워드")

    fake_client = AsyncMock()
    monkeypatch.setattr("app.services.auto_reply_service.get_authorized_client", AsyncMock(return_value=fake_client))
    fake_result = MagicMock(status="failed", error_message="network error")
    monkeypatch.setattr("app.services.auto_reply_service.deliver_message", AsyncMock(return_value=[fake_result]))

    await _handle_incoming_message(event, account.id)

    logs = await auto_reply_crud.list_logs(db_session, account.id)
    assert len(logs) == 1
    assert logs[0].status == "failed"
    assert logs[0].rule_id == rule.id
