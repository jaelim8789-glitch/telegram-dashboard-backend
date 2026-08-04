import mimetypes
import re
import time
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from app.core.limits import ALLOWED_MEDIA_CONTENT_TYPES, MAX_MEDIA_SIZE_BYTES
from app.core.logging import get_logger

logger = get_logger(__name__)

# Shared root for everything this app writes under ./media/<kind>/... . Broadcast
# attachments live in media/broadcasts (filesystem-path only -- consumed server-side
# by Telethon's send_file, never served over HTTP). Avatars live in media/avatars and
# ARE served over HTTP (see app.api.chats:get_chat_avatar), so they're kept in their
# own subdirectory, per-account/per-chat, rather than reusing the broadcasts folder.
MEDIA_BASE = Path(__file__).resolve().parent.parent.parent / "media"
MEDIA_ROOT = MEDIA_BASE / "broadcasts"
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

AVATAR_ROOT = MEDIA_BASE / "avatars"
AVATAR_ROOT.mkdir(parents=True, exist_ok=True)

# How long a saved avatar is considered "fresh enough" before get_chat_details()
# re-downloads it from Telegram. No prior on-disk-cache TTL convention exists
# elsewhere in this codebase (in-memory caches use 600s/3600s TTLs -- see
# telegram_actions._MEMBER_CACHE_TTL, api.link_preview._CACHE_TTL_SECONDS); 6 hours
# is a reasonable middle ground for a profile photo, which changes far less often
# than chat membership or link-preview metadata.
AVATAR_CACHE_TTL_SECONDS = 6 * 60 * 60

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _safe_id_component(value: str, label: str) -> str:
    """Reject anything that isn't a plain id -- no path separators, no '..'.

    account_id/chat_id are already typed/validated upstream by FastAPI path params
    (chat_id: int, account_id: str looked up via account_crud), but this is a second,
    cheap guard directly at the point where those values become part of a filesystem
    path, so a future caller that skips the DB lookup can't smuggle a traversal.
    """
    text = str(value)
    if not text or not _SAFE_ID_RE.match(text) or ".." in text:
        raise ValueError(f"Unsafe {label}: {value!r}")
    return text


def avatar_file_path(account_id: str, chat_id: str | int) -> Path:
    """Filesystem path for a cached avatar, per-account/per-chat so one account's
    session can never leak into another account's contact photos."""
    safe_account = _safe_id_component(account_id, "account_id")
    safe_chat = _safe_id_component(str(chat_id), "chat_id")
    directory = AVATAR_ROOT / safe_account
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{safe_chat}.jpg"


def avatar_is_fresh(path: Path) -> bool:
    """True if a cached avatar file exists and is within the TTL window."""
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < AVATAR_CACHE_TTL_SECONDS


def save_avatar_bytes(account_id: str, chat_id: str | int, data: bytes) -> Path:
    """Persist downloaded profile-photo bytes to the per-account/per-chat avatar path."""
    path = avatar_file_path(account_id, chat_id)
    path.write_bytes(data)
    return path


def avatar_url_path(account_id: str, chat_id: str | int) -> str:
    """URL path (route, not filesystem path) at which the avatar is servable.

    This is NOT a StaticFiles mount -- unlike media/broadcasts (which is never
    served over HTTP at all in this codebase), avatars must be access-controlled
    per-account like every other /api/chat-telegram/... route, so they're served
    through an authenticated FastAPI route rather than an open static mount.
    """
    safe_account = _safe_id_component(account_id, "account_id")
    safe_chat = _safe_id_component(str(chat_id), "chat_id")
    return f"/api/chat-telegram/accounts/{safe_account}/dialogs/{safe_chat}/avatar"

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
