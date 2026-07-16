"""Tests for app.services.ai_reply_service — standalone "AI Reply" drafting
and the Auto Reply AI-fallback persistence helper. Suggestion-only: neither
function ever sends a Telegram message.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.services.ai_reply_service as ai_reply_service_module
from app.models.auto_reply import AutoReplySuggestion
from app.services.ai_reply_service import generate_reply_suggestion, record_auto_reply_suggestion


@pytest.mark.asyncio
async def test_generate_reply_suggestion_returns_stripped_text(monkeypatch):
    fake = AsyncMock(return_value="  안녕하세요! 도와드릴게요.  ")
    monkeypatch.setattr(ai_reply_service_module, "_call_deepseek", fake)

    result = await generate_reply_suggestion("영업시간이 어떻게 되나요?")

    assert result == "안녕하세요! 도와드릴게요."
    fake.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_reply_suggestion_returns_none_on_deepseek_failure(monkeypatch):
    fake = AsyncMock(return_value=None)
    monkeypatch.setattr(ai_reply_service_module, "_call_deepseek", fake)

    result = await generate_reply_suggestion("영업시간이 어떻게 되나요?")

    assert result is None


@pytest.mark.asyncio
async def test_record_auto_reply_suggestion_persists_row(db_session, monkeypatch):
    fake = AsyncMock(return_value="네, 평일 오전 9시부터 오후 6시까지입니다.")
    monkeypatch.setattr(ai_reply_service_module, "_call_deepseek", fake)

    suggestion = await record_auto_reply_suggestion(
        db_session,
        account_id="acc-1",
        chat_id="chat-1",
        user_id="user-1",
        user_name="tester",
        trigger_message="영업시간이 어떻게 되나요?",
    )

    assert suggestion is not None
    assert suggestion.suggested_reply == "네, 평일 오전 9시부터 오후 6시까지입니다."
    assert suggestion.reviewed is False

    result = await db_session.execute(select(AutoReplySuggestion).where(AutoReplySuggestion.account_id == "acc-1"))
    rows = result.scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_record_auto_reply_suggestion_persists_nothing_on_failure(db_session, monkeypatch):
    fake = AsyncMock(return_value=None)
    monkeypatch.setattr(ai_reply_service_module, "_call_deepseek", fake)

    suggestion = await record_auto_reply_suggestion(
        db_session,
        account_id="acc-2",
        chat_id="chat-1",
        user_id="user-1",
        user_name=None,
        trigger_message="문의합니다",
    )

    assert suggestion is None
    result = await db_session.execute(select(AutoReplySuggestion).where(AutoReplySuggestion.account_id == "acc-2"))
    assert result.scalars().all() == []
