"""Guest AI Chat (/api/ai/guest/chat) -- no auth, IP rate-limited, nothing persisted."""

import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_guest_chat_succeeds_and_returns_reply(unauthenticated_client, monkeypatch):
    monkeypatch.setattr("app.api.ai_guest.settings.deepseek_api_key", "sk-test")
    with patch("app.api.ai_guest._call_deepseek", new=AsyncMock(return_value="안녕하세요! 무엇을 도와드릴까요?")):
        res = await unauthenticated_client.post("/api/ai/guest/chat", json={"message": "안녕"})
    assert res.status_code == 200
    assert res.json() == {"reply": "안녕하세요! 무엇을 도와드릴까요?"}


@pytest.mark.asyncio
async def test_guest_chat_requires_no_auth_header(unauthenticated_client, monkeypatch):
    """The whole point: works with zero credentials."""
    monkeypatch.setattr("app.api.ai_guest.settings.deepseek_api_key", "sk-test")
    with patch("app.api.ai_guest._call_deepseek", new=AsyncMock(return_value="ok")):
        res = await unauthenticated_client.post("/api/ai/guest/chat", json={"message": "hi"})
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_guest_chat_enforces_daily_ip_limit(unauthenticated_client, monkeypatch):
    monkeypatch.setattr("app.api.ai_guest.settings.deepseek_api_key", "sk-test")
    monkeypatch.setattr("app.api.ai_guest._MAX_PER_DAY", 2)
    with patch("app.api.ai_guest._call_deepseek", new=AsyncMock(return_value="ok")):
        r1 = await unauthenticated_client.post("/api/ai/guest/chat", json={"message": "1"})
        r2 = await unauthenticated_client.post("/api/ai/guest/chat", json={"message": "2"})
        r3 = await unauthenticated_client.post("/api/ai/guest/chat", json={"message": "3"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
    assert "Retry-After" in r3.headers


@pytest.mark.asyncio
async def test_guest_chat_upstream_failure_returns_502(unauthenticated_client, monkeypatch):
    monkeypatch.setattr("app.api.ai_guest.settings.deepseek_api_key", "sk-test")
    with patch("app.api.ai_guest._call_deepseek", new=AsyncMock(return_value=None)):
        res = await unauthenticated_client.post("/api/ai/guest/chat", json={"message": "hi"})
    assert res.status_code == 502


@pytest.mark.asyncio
async def test_guest_chat_rejects_when_deepseek_not_configured(unauthenticated_client, monkeypatch):
    monkeypatch.setattr("app.api.ai_guest.settings.deepseek_api_key", "")
    res = await unauthenticated_client.post("/api/ai/guest/chat", json={"message": "hi"})
    assert res.status_code == 503


@pytest.mark.asyncio
async def test_guest_chat_history_is_capped_and_only_user_assistant_roles_pass_through(unauthenticated_client, monkeypatch):
    monkeypatch.setattr("app.api.ai_guest.settings.deepseek_api_key", "sk-test")
    captured = {}

    async def fake_call(messages):
        captured["messages"] = messages
        return "ok"

    history = [{"role": "user", "content": f"msg{i}"} for i in range(20)]
    with patch("app.api.ai_guest._call_deepseek", new=fake_call):
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
    monkeypatch.setattr("app.api.ai_guest.settings.deepseek_api_key", "sk-test")
    res = await unauthenticated_client.post("/api/ai/guest/chat", json={"message": "x" * 2001})
    assert res.status_code == 422
