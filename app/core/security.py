import secrets
from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings

JWT_ALGORITHM = "HS256"
JWT_SUBJECT = "admin"


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
