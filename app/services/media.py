import mimetypes
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


def _extension_from_filename(filename: str | None) -> str | None:
    if not filename:
        return None
    lower = str(filename).lower()
    for ext in _EXTENSION_BY_CONTENT_TYPE.values():
        if lower.endswith(ext):
            return ext
    return None


def _resolve_content_type(upload: UploadFile) -> tuple[str, str]:
    content_type = str(getattr(upload, "content_type", "") or "").lower()
    if content_type in ALLOWED_MEDIA_CONTENT_TYPES:
        return content_type, _EXTENSION_BY_CONTENT_TYPE[content_type]

    extension = _extension_from_filename(getattr(upload, "filename", None))
    if extension is not None:
        inferred_type = "image/webp" if extension in {".webp", ".jpg", ".jpeg", ".png", ".gif"} else "video/mp4"
        return inferred_type, extension

    guessed_type, _ = mimetypes.guess_type(str(getattr(upload, "filename", "") or ""))
    if guessed_type and guessed_type.lower() in ALLOWED_MEDIA_CONTENT_TYPES:
        return guessed_type.lower(), _EXTENSION_BY_CONTENT_TYPE[guessed_type.lower()]

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="이미지 또는 영상 파일만 업로드할 수 있습니다 (jpeg/png/webp/gif/mp4/mov/avi/mkv).",
    )


def _safe_destination_filename(filename: str, content_type: str) -> Path:
    raw_name = Path(filename or "").name
    if not raw_name:
        raw_name = f"upload{_EXTENSION_BY_CONTENT_TYPE[content_type]}"
    stem = Path(raw_name).stem or "upload"
    extension = _EXTENSION_BY_CONTENT_TYPE.get(content_type)
    if extension is None:
        extension = ".bin"
    safe_name = f"{uuid.uuid4()}-{stem}{extension}"
    return MEDIA_ROOT / safe_name


async def save_broadcast_media(upload: UploadFile) -> str:
    content_type, extension = _resolve_content_type(upload)

    data = await upload.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="업로드할 파일이 비어 있습니다.")
    if len(data) > MAX_MEDIA_SIZE_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="파일은 500MB를 초과할 수 없습니다.")

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
