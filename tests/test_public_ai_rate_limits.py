"""Rate-limit coverage for public AI endpoints (/api/translate, /api/miniapp/*).

These endpoints are unauthenticated (translate happens during chat viewing;
miniapp is a preview surface) but must not become a free AI-cost abuse vector,
so they are rate-limited per client IP.
"""

import pytest

import app.api.translate as translate_module
import app.routes.miniapp_routes as miniapp_module


async def _stub_translate(*args, **kwargs):
    return "translated"


async def _stub_chat(messages):
    return "hello"


@pytest.mark.asyncio
async def test_translate_rate_limit(client, monkeypatch):
    monkeypatch.setattr(translate_module, "translate_text", _stub_translate)
    payload = {"text": "hello", "target_lang": "ko"}
    for _ in range(20):
        r = await client.post("/api/translate", json=payload)
        assert r.status_code == 200, r.text
    r = await client.post("/api/translate", json=payload)
    assert r.status_code == 429
    assert r.json()["detail"] == "Too many translation requests. Please try again later."
    assert "retry-after" in r.headers


@pytest.mark.asyncio
async def test_miniapp_chat_rate_limit(client, monkeypatch):
    monkeypatch.setattr(miniapp_module, "chat_with_deepseek", _stub_chat)
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    for _ in range(10):
        r = await client.post("/api/miniapp/chat", json=payload)
        assert r.status_code == 200, r.text
    r = await client.post("/api/miniapp/chat", json=payload)
    assert r.status_code == 429
    assert "요청이 너무 많습니다" in r.json()["detail"]
    assert "retry-after" in r.headers


@pytest.mark.asyncio
async def test_miniapp_pixel_offices_rate_limit(client):
    for _ in range(30):
        r = await client.get("/api/miniapp/pixel-offices")
        assert r.status_code == 200, r.text
    r = await client.get("/api/miniapp/pixel-offices")
    assert r.status_code == 429
    assert "요청이 너무 많습니다" in r.json()["detail"]
    assert "retry-after" in r.headers
