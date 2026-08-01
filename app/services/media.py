import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from app.core.limits import ALLOWED_MEDIA_CONTENT_TYPES, MAX_MEDIA_SIZE_BYTES
from app.core.logging import get_logger

logger = get_logger(__name__)

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


def infer_media_type(filename: str) -> str:
    lower = (filename or "").lower()
    if lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return "photo"
    if lower.endswith((".mp4", ".mov", ".mkv", ".avi", ".webm")):
        return "video"
    return "document"


def _safe_destination_filename(filename: str, content_type: str) -> Path:
    raw_name = Path(filename or "").name
    if not raw_name:
        raw_name = f"upload{_EXTENSION_BY_CONTENT_TYPE[content_type]}"
    stem = Path(raw_name).stem or "upload"
    extension = _EXTENSION_BY_CONTENT_TYPE[content_type]
    safe_name = f"{uuid.uuid4()}-{stem}{extension}"
    return MEDIA_ROOT / safe_name


async def save_broadcast_media(upload: UploadFile) -> str:
    content_type = (upload.content_type or "").lower()
    if content_type not in ALLOWED_MEDIA_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="이미지 또는 영상 파일만 업로드할 수 있습니다 (jpeg/png/webp/gif/mp4/mov/avi/mkv).",
        )

    data = await upload.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="업로드할 파일이 비어 있습니다.")
    if len(data) > MAX_MEDIA_SIZE_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="파일은 500MB를 초과할 수 없습니다.")

    extension = _EXTENSION_BY_CONTENT_TYPE[content_type]
    destination = _safe_destination_filename(upload.filename or "", content_type)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)

    logger.info(
        "broadcast_media_saved",
        filename=upload.filename,
        content_type=content_type,
        size=len(data),
        destination=str(destination),
    )
    return str(destination)
