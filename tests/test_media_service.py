import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.services.media import MEDIA_ROOT, save_broadcast_media


@pytest.mark.asyncio
async def test_save_broadcast_media_valid_image():
    """Valid image file upload test"""
    # Create a mock UploadFile object with the required attributes
    mock_file = AsyncMock()
    mock_file.content_type = "image/jpeg"
    mock_file.read = AsyncMock(return_value=b"fake image data")
    mock_file.filename = "test.jpg"
    
    result_path = await save_broadcast_media(mock_file)
    
    # Verify the result
    assert result_path.startswith(str(MEDIA_ROOT))
    assert result_path.endswith('.jpg')
    
    # Cleanup
    if os.path.exists(result_path):
        os.remove(result_path)


@pytest.mark.asyncio
async def test_save_broadcast_media_invalid_content_type():
    """Test invalid content type raises exception"""
    mock_file = AsyncMock()
    mock_file.content_type = "application/pdf"
    mock_file.read = AsyncMock(return_value=b"fake pdf data")
    
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        await save_broadcast_media(mock_file)
    
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_save_broadcast_media_exceeds_size():
    """Test file size limit"""
    large_data = b"x" * (1024 * 1024 * 501)  # 501 MB, exceeding the limit
    mock_file = AsyncMock()
    mock_file.content_type = "image/jpeg"
    mock_file.read = AsyncMock(return_value=large_data)
    
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        await save_broadcast_media(mock_file)
    
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_save_broadcast_media_uses_filename_extension_when_content_type_is_missing(monkeypatch, tmp_path):
    """Filename-based extension fallback should be used when content-type is unavailable."""
    monkeypatch.setattr("app.services.media.MEDIA_ROOT", tmp_path / "media")

    mock_file = AsyncMock()
    mock_file.content_type = None
    mock_file.read = AsyncMock(return_value=b"fallback data")
    mock_file.filename = "sample.webp"

    result_path = await save_broadcast_media(mock_file)

    assert result_path.endswith(".webp")
    assert Path(result_path).exists()
    assert Path(result_path).read_bytes() == b"fallback data"