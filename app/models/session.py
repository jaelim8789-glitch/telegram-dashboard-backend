import secrets
from datetime import datetime, timezone, timedelta

from sqlalchemy import String, DateTime, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base

SESSION_TOKEN_BYTES = 32
SESSION_PREFIX = "sx-"
SESSION_EXPIRE_DAYS = 30


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    api_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def generate_session_token() -> str:
    return f"{SESSION_PREFIX}{secrets.token_urlsafe(SESSION_TOKEN_BYTES)}"


def hash_session_token(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _session_expires_at() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=SESSION_EXPIRE_DAYS)
