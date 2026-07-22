import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings
from app.core.limits import OTP_CODE_LENGTH

JWT_ALGORITHM = "HS256"
JWT_SUBJECT = "admin"
USER_JWT_SUBJECT_PREFIX = "user:"

# Separate secret for user tokens. Defaults to admin_jwt_secret for backward
# compatibility but reads from JWT_USER_SECRET env var when set, so user tokens
# can be revoked independently by rotating the user secret without invalidating
# admin sessions.
def _get_user_jwt_secret() -> str:
    import os
    return os.getenv("JWT_USER_SECRET", settings.admin_jwt_secret)


def verify_admin_credentials(username: str, password: str) -> bool:
    # Constant-time comparisons — a naive `==` leaks timing information that could help
    # an attacker guess the credentials character-by-character. compare_digest requires
    # bytes or ASCII-only str (raises TypeError otherwise), so encode first — a wrong
    # password containing non-ASCII characters (e.g. Korean) would otherwise 500 instead
    # of cleanly reporting "invalid credentials".
    return secrets.compare_digest(
        username.encode("utf-8"), settings.admin_username.encode("utf-8")
    ) and secrets.compare_digest(password.encode("utf-8"), settings.admin_password.encode("utf-8"))


def create_access_token() -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.admin_jwt_expire_minutes)
    payload = {"sub": JWT_SUBJECT, "exp": expires_at}
    return jwt.encode(payload, settings.admin_jwt_secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> bool:
    """Returns True for a valid, unexpired admin token. Raises jwt exceptions otherwise."""
    payload = jwt.decode(token, settings.admin_jwt_secret, algorithms=[JWT_ALGORITHM])
    return payload.get("sub") == JWT_SUBJECT


def generate_api_key() -> str:
    return f"sk-{secrets.token_hex(16)}"  # 32 hex chars, per spec


def mask_api_key(key: str) -> str:
    return f"{key[:7]}...{key[-4:]}"


def generate_user_api_key() -> str:
    return f"sk-{secrets.token_urlsafe(32)}"


def hash_api_key(raw_key: str) -> str:
    """SHA-256 is fine here (not bcrypt/argon2) — this hashes a high-entropy random
    token, not a human-chosen password, so there's no offline-guessing risk to slow down."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def create_user_access_token(user_id: str) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.admin_jwt_expire_minutes)
    payload = {"sub": f"{USER_JWT_SUBJECT_PREFIX}{user_id}", "exp": expires_at}
    return jwt.encode(payload, _get_user_jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_user_access_token(token: str) -> dict | None:
    """Returns the payload dict for a valid, unexpired user token, or None if it's
    a well-formed token for something else (e.g. an admin token). Raises jwt exceptions
    for a malformed/expired/tampered token."""
    payload = jwt.decode(token, _get_user_jwt_secret(), algorithms=[JWT_ALGORITHM])
    sub = payload.get("sub", "")
    if isinstance(sub, str) and sub.startswith(USER_JWT_SUBJECT_PREFIX):
        return payload
    return None


def generate_otp_code() -> str:
    return f"{secrets.randbelow(10**OTP_CODE_LENGTH):0{OTP_CODE_LENGTH}d}"


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def hash_otp_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()
