import pytest
import os
from pathlib import Path
from unittest.mock import AsyncMock

from app.services.media import save_broadcast_media, MEDIA_ROOT

from app.services.media import save_broadcast_media, MEDIA_ROOT


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