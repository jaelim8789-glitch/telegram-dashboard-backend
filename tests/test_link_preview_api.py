"""Tests for GET /api/link-preview  SSRF hardening, happy path, and rate limiting."""

import pytest

import app.api.link_preview as link_preview_module
from app.services.link_preview_service import LinkPreview, LinkPreviewError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata endpoint
        "http://localhost/",
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "ftp://example.com/",  # disallowed scheme
        "not-a-url",
    ],
)
async def test_link_preview_rejects_ssrf_targets(client, url):
    r = await client.get("/api/link-preview", params={"url": url})
    assert r.status_code in (400, 415, 422), r.text
    assert r.status_code < 500


@pytest.mark.asyncio
async def test_link_preview_happy_path(client, monkeypatch):
    async def _stub_fetch(url: str) -> LinkPreview:
        return LinkPreview(
            url=url,
            title="Example title",
            description="Example description",
            image="https://example.com/thumb.png",
        )

    monkeypatch.setattr(link_preview_module, "fetch_link_preview", _stub_fetch)

    r = await client.get("/api/link-preview", params={"url": "https://example.com/article"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Example title"
    assert body["description"] == "Example description"
    assert body["image"] == "https://example.com/thumb.png"


@pytest.mark.asyncio
async def test_link_preview_propagates_fetch_errors(client, monkeypatch):
    async def _stub_fetch(url: str):
        raise LinkPreviewError("blocked", status_code=400)

    monkeypatch.setattr(link_preview_module, "fetch_link_preview", _stub_fetch)

    r = await client.get("/api/link-preview", params={"url": "https://example.com/"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_link_preview_rate_limit(client, monkeypatch):
    async def _stub_fetch(url: str) -> LinkPreview:
        return LinkPreview(url=url, title="t")

    monkeypatch.setattr(link_preview_module, "fetch_link_preview", _stub_fetch)

    for _ in range(30):
        r = await client.get("/api/link-preview", params={"url": "https://example.com/"})
        assert r.status_code == 200, r.text
    r = await client.get("/api/link-preview", params={"url": "https://example.com/"})
    assert r.status_code == 429
    assert "retry-after" in r.headers
