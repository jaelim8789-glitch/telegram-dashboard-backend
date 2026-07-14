import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from app.core.limits import ALLOWED_MEDIA_CONTENT_TYPES, MAX_MEDIA_SIZE_BYTES

MEDIA_ROOT = Path(__file__).resolve().parent.parent.parent / "media" / "broadcasts"
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

_EXTENSION_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/x-matroska": ".mkv",
}


async def save_broadcast_media(upload: UploadFile) -> str:
    if upload.content_type not in ALLOWED_MEDIA_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="이미지 또는 영상 파일만 업로드할 수 있습니다 (jpeg/png/webp/gif/mp4/mov/avi/mkv).",
        )

    data = await upload.read()
    if len(data) > MAX_MEDIA_SIZE_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="파일은 500MB를 초과할 수 없습니다.")

    extension = _EXTENSION_BY_CONTENT_TYPE[upload.content_type]
    filename = f"{uuid.uuid4()}{extension}"
    destination = MEDIA_ROOT / filename
    destination.write_bytes(data)
    return str(destination)
