"""Quick API integration test for content studio generate endpoint."""

import pytest
from unittest.mock import patch

from app.schemas.content_studio import ContentGenerateRequest


@pytest.mark.asyncio
async def test_generate_endpoint_returns_ok(client):
    payload = ContentGenerateRequest(
        content_type="promotional",
        tone="short",
        topic=" ",
        context=" ",
    )

    fake_reply = "     !    !"
    with patch("app.api.content_studio.generate_content", return_value=(fake_reply, 42, "cs-123")) as mock_gen:
        with patch("app.services.ai_core_service.check_ai_quota", return_value=(True, "")):
            response = await client.post("/api/ai/content-studio/generate", json=payload.model_dump())

    assert response.status_code == 200
    data = response.json()
    assert data["content_type"] == "promotional"
    assert data["tone"] == "short"
    assert data["generated_content"] == fake_reply
    assert data["tokens_used"] == 42
    assert data["style_profile_id"] is None
    assert data["content_studio_content_id"] == "cs-123"

    mock_gen.assert_called_once()
    call_kwargs = mock_gen.call_args.kwargs
    assert call_kwargs["content_type"] == "promotional"
    assert call_kwargs["tone"] == "short"
    assert call_kwargs["topic"] == " "
    assert call_kwargs["context"] == " "
    assert call_kwargs["style_profile_id"] is None


@pytest.mark.asyncio
async def test_generate_endpoint_with_style_profile(client):
    payload = ContentGenerateRequest(
        content_type="engagement",
        tone="emotional",
        topic=" ",
        style_profile_id="style-123",
    )

    fake_reply = "   ... !"
    with patch("app.api.content_studio.generate_content", return_value=(fake_reply, 30, "cs-456")) as mock_gen:
        with patch("app.services.ai_core_service.check_ai_quota", return_value=(True, "")):
            response = await client.post("/api/ai/content-studio/generate", json=payload.model_dump())

    assert response.status_code == 200
    data = response.json()
    assert data["style_profile_id"] == "style-123"
    assert data["content_studio_content_id"] == "cs-456"
    mock_gen.assert_called_once()
    assert mock_gen.call_args.kwargs["style_profile_id"] == "style-123"
