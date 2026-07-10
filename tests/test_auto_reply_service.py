from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.crud import account as account_crud
from app.crud import auto_reply as auto_reply_crud
from app.models.auto_reply import AutoReplyLog, AutoReplyRule
from app.schemas.account import AccountCreate
from app.schemas.auto_reply import AutoReplyRuleCreate
from app.services.auto_reply_service import _handle_incoming_message, _matches


def _fake_event(text: str, *, out: bool = False, sender_id: int = 111, chat_id: int = 222, username="tester"):
    sender = SimpleNamespace(username=username, first_name="Tester")
    return SimpleNamespace(
        out=out,
        raw_text=text,
        sender_id=sender_id,
        chat_id=chat_id,
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
        name=overrides.pop("name", "가격 문의"),
        match_type=overrides.pop("match_type", "keyword"),
        match_value=overrides.pop("match_value", "가격"),
        reply_content=overrides.pop("reply_content", "가격은 10,000원입니다"),
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
        trigger_message="가격 얼마예요",
        reply_sent="가격은 10,000원입니다",
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
    assert _matches(rule, "what's the price?")
    assert not _matches(rule, "hello there")


def test_matches_exact_requires_full_match_after_strip():
    rule = SimpleNamespace(match_type="exact", match_value="영업시간")
    assert _matches(rule, "  영업시간  ")
    assert not _matches(rule, "영업시간 알려주세요")


@pytest.mark.asyncio
async def test_handle_incoming_message_sends_reply_and_logs_success(db_session):
    account = await _make_account(db_session)
    rule = await _make_rule(db_session, account.id)
    event = _fake_event("가격이 얼마인가요?")

    await _handle_incoming_message(event, account.id)

    event.reply.assert_awaited_once_with(rule.reply_content)
    logs = await auto_reply_crud.list_logs(db_session, account.id)
    assert len(logs) == 1
    assert logs[0].status == "success"
    assert logs[0].reply_sent == rule.reply_content


@pytest.mark.asyncio
async def test_handle_incoming_message_ignores_own_outgoing_messages(db_session):
    account = await _make_account(db_session)
    await _make_rule(db_session, account.id)
    event = _fake_event("가격 안내드립니다", out=True)

    await _handle_incoming_message(event, account.id)

    event.reply.assert_not_called()
    assert await auto_reply_crud.list_logs(db_session, account.id) == []


@pytest.mark.asyncio
async def test_handle_incoming_message_skips_when_master_switch_off(db_session):
    account = await _make_account(db_session, auto_reply_enabled=False)
    await _make_rule(db_session, account.id)
    event = _fake_event("가격 알려주세요")

    await _handle_incoming_message(event, account.id)

    event.reply.assert_not_called()
    assert await auto_reply_crud.list_logs(db_session, account.id) == []


@pytest.mark.asyncio
async def test_handle_incoming_message_no_keyword_match_does_nothing(db_session):
    account = await _make_account(db_session)
    await _make_rule(db_session, account.id)
    event = _fake_event("안녕하세요")

    await _handle_incoming_message(event, account.id)

    event.reply.assert_not_called()
    assert await auto_reply_crud.list_logs(db_session, account.id) == []


@pytest.mark.asyncio
async def test_handle_incoming_message_cooldown_blocks_repeat_from_same_user(db_session):
    account = await _make_account(db_session)
    rule = await _make_rule(db_session, account.id, cooldown_hours=1)
    await _seed_log(db_session, rule.id, account.id, user_id="111", status="success")

    event = _fake_event("가격 다시 알려주세요", sender_id=111)
    await _handle_incoming_message(event, account.id)

    event.reply.assert_not_called()
    logs = await auto_reply_crud.list_logs(db_session, account.id)
    assert len(logs) == 2
    rate_limited_logs = [log for log in logs if log.status == "rate_limited"]
    assert len(rate_limited_logs) == 1, f"Expected 1 rate_limited log, got {len(rate_limited_logs)}: {[(l.status, l.created_at) for l in logs]}"
    success_logs = [log for log in logs if log.status == "success"]
    assert len(success_logs) == 1


@pytest.mark.asyncio
async def test_handle_incoming_message_daily_limit_blocks_new_user_once_reached(db_session):
    account = await _make_account(db_session)
    rule = await _make_rule(db_session, account.id, max_replies_per_day=1)
    # One successful reply already sent today, to a different user — daily limit is
    # rule-wide, not per-user, so a brand new user (no cooldown history) still gets blocked.
    await _seed_log(db_session, rule.id, account.id, user_id="999", status="success")

    event = _fake_event("가격 알려주세요", sender_id=111)
    await _handle_incoming_message(event, account.id)

    event.reply.assert_not_called()
    logs = await auto_reply_crud.list_logs(db_session, account.id)
    rate_limited = [log for log in logs if log.status == "rate_limited"]
    assert len(rate_limited) == 1
    assert rate_limited[0].user_id == "111"


@pytest.mark.asyncio
async def test_handle_incoming_message_send_failure_logs_failed_status(db_session):
    account = await _make_account(db_session)
    rule = await _make_rule(db_session, account.id)
    event = _fake_event("가격이 얼마인가요?")
    event.reply = AsyncMock(side_effect=RuntimeError("network error"))

    await _handle_incoming_message(event, account.id)

    logs = await auto_reply_crud.list_logs(db_session, account.id)
    assert len(logs) == 1
    assert logs[0].status == "failed"
    assert logs[0].rule_id == rule.id
