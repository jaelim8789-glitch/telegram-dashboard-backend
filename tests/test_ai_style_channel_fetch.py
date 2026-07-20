"""Unit tests for ai_style_service._fetch_channel_messages.

Verifies:
1. out=True messages kept, out=False skipped.
2. 8000-character cap and truncation.
3. Empty result raises ValueError.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.ai_style_service as svc
from app.services.ai_style_service import _fetch_channel_messages


def _fake_message(text: str, out: bool = True):
    msg = MagicMock()
    msg.out = out
    msg.text = text
    return msg


import app.database as database


class _AsyncCtxManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *args):
        pass


@pytest.fixture(autouse=True)
def _patch_deps(monkeypatch):
    """Patch all external dependencies so the test never touches DB or Telethon."""
    mock_session = AsyncMock()
    mock_session_maker = MagicMock(return_value=_AsyncCtxManager(mock_session))
    monkeypatch.setattr(database, "async_session_maker", mock_session_maker)
    monkeypatch.setattr(svc.account_crud, "get_account", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(svc, "get_authorized_client", AsyncMock(return_value=MagicMock()))


@pytest.mark.asyncio
async def test_keeps_only_out_true_messages():
    """out=True messages are kept; out=False or no-text messages are skipped."""
    fake_client = AsyncMock()
    fake_client.is_connected.return_value = True
    fake_client.get_messages = AsyncMock(return_value=[
        _fake_message("내가 쓴 글1", out=True),
        _fake_message("남이 쓴 글", out=False),
        _fake_message("내가 쓴 글2", out=True),
        _fake_message(None, out=True),          # no text → skip
        _fake_message("", out=True),             # empty after strip → skip
        _fake_message("남의 글2", out=False),
        _fake_message("내가 쓴 글3", out=True),
    ])
    svc.get_authorized_client = AsyncMock(return_value=fake_client)

    result = await _fetch_channel_messages("acc-1", "-1001234567890", limit=50)

    assert "내가 쓴 글1" in result
    assert "내가 쓴 글2" in result
    assert "내가 쓴 글3" in result
    assert "남이 쓴 글" not in result
    assert "남의 글2" not in result


@pytest.mark.asyncio
async def test_respects_8000_char_cap():
    """Total collected text does not exceed _MAX_CHANNEL_CHARS (8000)."""
    fake_client = AsyncMock()
    fake_client.is_connected.return_value = True

    long_body = "가" * 7000
    mid_body = "나" * 3000
    fake_client.get_messages = AsyncMock(return_value=[
        _fake_message(long_body, out=True),
        _fake_message(mid_body, out=True),
    ])
    svc.get_authorized_client = AsyncMock(return_value=fake_client)

    result = await _fetch_channel_messages("acc-1", "-1001234567890", limit=50)

    # First message fits entirely (7000 < 8000); second is truncated to ~1000
    assert long_body in result
    assert "나" * 1000 in result
    assert len(result) <= 8000 + len("\n\n---\n\n")  # separator overhead


@pytest.mark.asyncio
async def test_raises_value_error_on_empty_result():
    """ValueError raised when no collectable messages found."""
    fake_client = AsyncMock()
    fake_client.is_connected.return_value = True
    fake_client.get_messages = AsyncMock(return_value=[
        _fake_message("남의 글", out=False),
        _fake_message(None, out=True),
    ])
    svc.get_authorized_client = AsyncMock(return_value=fake_client)

    with pytest.raises(ValueError, match="분석할 텍스트 메시지를 찾을 수 없습니다"):
        await _fetch_channel_messages("acc-1", "-1001234567890", limit=50)
