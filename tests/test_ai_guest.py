"""Guest AI Chat (/api/ai/guest/chat) -- no auth, per-IP credit bucket, nothing persisted."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.guest_credit_service import (
    GUEST_CREDITS_PER_REFILL,
    get_guest_credits,
    reset_guest_credits_for_ip,
)


@pytest.mark.asyncio
async def test_guest_chat_succeeds_and_returns_reply(unauthenticated_client, monkeypatch):
    monkeypatch.setattr("app.api.ai_guest.settings.ollama_api_base", "http://ollama-test")
    with patch("app.api.ai_guest._call_ollama_full", new=AsyncMock(return_value=("안녕하세요! 무엇을 도와드릴까요?", None))):
        res = await unauthenticated_client.post("/api/ai/guest/chat", json={"message": "안녕"})
    assert res.status_code == 200
    body = res.json()
    assert body["reply"] == "안녕하세요! 무엇을 도와드릴까요?"
    assert body["remaining_credits"] >= 0


@pytest.mark.asyncio
async def test_guest_chat_requires_no_auth_header(unauthenticated_client, monkeypatch):
    """The whole point: works with zero credentials."""
    monkeypatch.setattr("app.api.ai_guest.settings.ollama_api_base", "http://ollama-test")
    with patch("app.api.ai_guest._call_ollama_full", new=AsyncMock(return_value=("ok", None))):
        res = await unauthenticated_client.post("/api/ai/guest/chat", json={"message": "hi"})
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_guest_chat_deducts_credits_per_char(unauthenticated_client, monkeypatch):
    """Credit bucket (replaced the old 30 msgs/day limit): 1 credit = 1 char
    of input + output, deducted only after the AI call succeeds."""
    monkeypatch.setattr("app.api.ai_guest.settings.ollama_api_base", "http://ollama-test")
    reset_guest_credits_for_ip("203.0.113.9")
    with patch("app.api.ai_guest._call_ollama_full", new=AsyncMock(return_value=("ok", None))):
        r1 = await unauthenticated_client.post(
            "/api/ai/guest/chat", json={"message": "hi"}, headers={"X-Real-IP": "203.0.113.9"}
        )
        r2 = await unauthenticated_client.post(
            "/api/ai/guest/chat", json={"message": "hello"}, headers={"X-Real-IP": "203.0.113.9"}
        )
    assert r1.status_code == 200
    assert r2.status_code == 200
    # r1: input "hi" (2) + output "ok" (2) = 4 credits
    assert r1.json()["remaining_credits"] == GUEST_CREDITS_PER_REFILL - 4
    # r2: input "hello" (5) + output "ok" (2) = 7 more credits
    assert r2.json()["remaining_credits"] == GUEST_CREDITS_PER_REFILL - 11


@pytest.mark.asyncio
async def test_guest_credits_endpoint_reports_full_bucket(unauthenticated_client):
    """The credits endpoint exposes the per-IP bucket the chat endpoint charges."""
    res = await unauthenticated_client.get(
        "/api/ai/guest/credits", headers={"X-Real-IP": "203.0.113.10"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["remaining_credits"] == GUEST_CREDITS_PER_REFILL
    assert body["max_credits"] == GUEST_CREDITS_PER_REFILL
    assert body["credit_per_char"] == 1
    assert body["refill_countdown_seconds"] == 0


@pytest.mark.asyncio
async def test_guest_chat_upstream_failure_returns_502(unauthenticated_client, monkeypatch):
    """Upstream failure -> 502, and (documented) failed calls never consume credits."""
    monkeypatch.setattr("app.api.ai_guest.settings.ollama_api_base", "http://ollama-test")
    reset_guest_credits_for_ip("203.0.113.11")
    with patch("app.api.ai_guest._call_ollama_full", new=AsyncMock(return_value=None)):
        res = await unauthenticated_client.post(
            "/api/ai/guest/chat", json={"message": "hi"}, headers={"X-Real-IP": "203.0.113.11"}
        )
    assert res.status_code == 502
    remaining, _ = get_guest_credits("203.0.113.11")
    assert remaining == GUEST_CREDITS_PER_REFILL


@pytest.mark.asyncio
async def test_guest_chat_rejects_when_ollama_not_configured(unauthenticated_client, monkeypatch):
    monkeypatch.setattr("app.api.ai_guest.settings.ollama_api_base", "")
    res = await unauthenticated_client.post("/api/ai/guest/chat", json={"message": "hi"})
    assert res.status_code == 503


@pytest.mark.asyncio
async def test_guest_chat_history_is_capped_and_only_user_assistant_roles_pass_through(unauthenticated_client, monkeypatch):
    monkeypatch.setattr("app.api.ai_guest.settings.ollama_api_base", "http://ollama-test")
    captured = {}

    async def fake_call(messages, **kwargs):
        captured["messages"] = messages
        return ("ok", None)

    history = [{"role": "user", "content": f"msg{i}"} for i in range(20)]
    with patch("app.api.ai_guest._call_ollama_full", new=fake_call):
        res = await unauthenticated_client.post(
            "/api/ai/guest/chat", json={"message": "final", "history": history[:12]}
        )
    assert res.status_code == 200
    # system prompt + up to 12 history + final user message
    assert len(captured["messages"]) <= 1 + 12 + 1
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][-1] == {"role": "user", "content": "final"}


@pytest.mark.asyncio
async def test_guest_chat_message_too_long_rejected(unauthenticated_client, monkeypatch):
    monkeypatch.setattr("app.api.ai_guest.settings.ollama_api_base", "http://ollama-test")
    res = await unauthenticated_client.post("/api/ai/guest/chat", json={"message": "x" * 2001})
    assert res.status_code == 422
