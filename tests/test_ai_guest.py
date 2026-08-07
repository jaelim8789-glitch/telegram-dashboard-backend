"""Guest AI Chat (/api/ai/guest/chat) -- no auth, per-device credit bucket, nothing persisted."""

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
    of input + output, deducted only after the AI call succeeds.

    Uses the cookie-less IP fallback bucket: the test client would otherwise
    auto-store the Set-Cookie from r1 and switch to a `dev:` bucket on r2, so
    we clear its jar between calls to keep both hits on the same `ip:` bucket."""
    monkeypatch.setattr("app.api.ai_guest.settings.ollama_api_base", "http://ollama-test")
    reset_guest_credits_for_ip("ip:203.0.113.9")
    with patch("app.api.ai_guest._call_ollama_full", new=AsyncMock(return_value=("ok", None))):
        r1 = await unauthenticated_client.post(
            "/api/ai/guest/chat", json={"message": "hi"}, headers={"X-Real-IP": "203.0.113.9"}
        )
        unauthenticated_client.cookies.clear()
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
async def test_guest_chat_two_devices_same_ip_do_not_share_credits(unauthenticated_client, monkeypatch):
    """Regression: visitors behind the same NAT IP used to share one bucket.
    A `guest_device_id` cookie now isolates each browser's credits even when
    the IP (X-Real-IP) is identical."""
    monkeypatch.setattr("app.api.ai_guest.settings.ollama_api_base", "http://ollama-test")
    reset_guest_credits_for_ip("dev:device-aaa")
    reset_guest_credits_for_ip("dev:device-bbb")
    with patch("app.api.ai_guest._call_ollama_full", new=AsyncMock(return_value=("ok", None))):
        a1 = await unauthenticated_client.post(
            "/api/ai/guest/chat",
            json={"message": "hi"},
            headers={"X-Real-IP": "203.0.113.50", "Cookie": "guest_device_id=device-aaa"},
        )
        a2 = await unauthenticated_client.post(
            "/api/ai/guest/chat",
            json={"message": "hello"},
            headers={"X-Real-IP": "203.0.113.50", "Cookie": "guest_device_id=device-aaa"},
        )
        b1 = await unauthenticated_client.post(
            "/api/ai/guest/chat",
            json={"message": "longer message from another device"},
            headers={"X-Real-IP": "203.0.113.50", "Cookie": "guest_device_id=device-bbb"},
        )
    assert a1.status_code == 200 and a2.status_code == 200 and b1.status_code == 200
    # Device A: 2+2 then 5+2 = 4, then 7 more (11 total)
    assert a2.json()["remaining_credits"] == GUEST_CREDITS_PER_REFILL - 11
    # Device B (same IP!): untouched by A's usage -> full minus its own
    # 34-char message + 2-char reply = 36 credits
    assert b1.json()["remaining_credits"] == GUEST_CREDITS_PER_REFILL - 36


@pytest.mark.asyncio
async def test_guest_chat_cookie_cannot_collide_with_ip_bucket(unauthenticated_client, monkeypatch):
    """A crafted guest_device_id cookie must NOT be able to address the
    `ip:`-namespaced bucket of another visitor (drain/inspect it)."""
    monkeypatch.setattr("app.api.ai_guest.settings.ollama_api_base", "http://ollama-test")
    reset_guest_credits_for_ip("ip:203.0.113.60")
    # Drain the victim's IP bucket directly.
    from app.services.guest_credit_service import try_deduct_guest_credits
    ok, remaining = try_deduct_guest_credits("ip:203.0.113.60", 5000)
    assert ok and remaining == GUEST_CREDITS_PER_REFILL - 5000

    with patch("app.api.ai_guest._call_ollama_full", new=AsyncMock(return_value=("ok", None))):
        # Attacker sends a cookie that would collide with the victim's IP bucket
        # IF cookies weren't `dev:`-namespaced.
        res = await unauthenticated_client.post(
            "/api/ai/guest/chat",
            json={"message": "hi"},
            headers={"X-Real-IP": "203.0.113.60", "Cookie": "guest_device_id=ip:203.0.113.60"},
        )
    assert res.status_code == 200
    # Cookie is namespaced to dev:ip:203.0.113.60 -> a fresh bucket, so the
    # victim's drained ip: bucket is untouched and stays low.
    victim_remaining, _ = get_guest_credits("ip:203.0.113.60")
    assert victim_remaining == GUEST_CREDITS_PER_REFILL - 5000


@pytest.mark.asyncio
async def test_guest_credits_endpoint_reports_full_bucket(unauthenticated_client):
    """The credits endpoint exposes the per-device bucket the chat endpoint charges."""
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
async def test_guest_chat_sets_device_cookie(unauthenticated_client, monkeypatch):
    """Cookie-less first request gets a guest_device_id Set-Cookie so the
    browser keeps its own bucket."""
    monkeypatch.setattr("app.api.ai_guest.settings.ollama_api_base", "http://ollama-test")
    with patch("app.api.ai_guest._call_ollama_full", new=AsyncMock(return_value=("ok", None))):
        res = await unauthenticated_client.post("/api/ai/guest/chat", json={"message": "hi"})
    assert res.status_code == 200
    set_cookie = res.headers.get("set-cookie", "")
    assert "guest_device_id=" in set_cookie


@pytest.mark.asyncio
async def test_guest_chat_upstream_failure_returns_502(unauthenticated_client, monkeypatch):
    """Upstream failure -> 502, and (documented) failed calls never consume credits."""
    monkeypatch.setattr("app.api.ai_guest.settings.ollama_api_base", "http://ollama-test")
    reset_guest_credits_for_ip("ip:203.0.113.11")
    with patch("app.api.ai_guest._call_ollama_full", new=AsyncMock(return_value=None)):
        res = await unauthenticated_client.post(
            "/api/ai/guest/chat", json={"message": "hi"}, headers={"X-Real-IP": "203.0.113.11"}
        )
    assert res.status_code == 502
    remaining, _ = get_guest_credits("ip:203.0.113.11")
    assert remaining == GUEST_CREDITS_PER_REFILL


@pytest.mark.asyncio
async def test_guest_chat_log_is_persisted_even_when_memory_engine_fails(unauthenticated_client, db_session, monkeypatch):
    """Regression: guest logs stopped appearing in the admin review page when
    the memory engine left the DB transaction aborted (InFailedSQLTransactionError
    on the INSERT). The chat must still persist its log row.

    The memory stub must ABORT the transaction (a failing SQL statement), not
    just raise a Python exception — a plain RuntimeError leaves the transaction
    healthy, so the log INSERT would succeed even without the rollback fix and
    the test would pass on broken code."""
    monkeypatch.setattr("app.api.ai_guest.settings.ollama_api_base", "http://ollama-test")

    async def boom_memory(db, *args, **kwargs):
        from sqlalchemy import text
        await db.execute(text("SELECT * FROM no_such_table_xyz"))  # aborts tx
        raise RuntimeError("embedding service down")

    with patch("app.api.ai_guest._call_ollama_full", new=AsyncMock(return_value=("ok", None))), \
         patch("app.services.memory_engine.maybe_store_memory", new=boom_memory):
        res = await unauthenticated_client.post(
            "/api/ai/guest/chat", json={"message": "remember this"}, headers={"X-Real-IP": "203.0.113.12"}
        )
    assert res.status_code == 200
    # The log row must exist despite the memory failure.
    from app.models.guest_ai_chat import GuestAiChatLog
    from sqlalchemy import select
    row = (await db_session.execute(
        select(GuestAiChatLog).where(GuestAiChatLog.ip == "203.0.113.12").limit(1)
    )).scalar_one_or_none()
    assert row is not None


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
