import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    phone: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    # SHA-256 hex digest of the issued API key — the raw key is shown once, at issuance,
    # and never stored or logged.
    api_key_hash: Mapped[str | None] = mapped_column(String(128), unique=True, index=True, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PhoneVerification(Base):
    """One row per phone with a currently-pending code — a new send-code request
    replaces any existing row outright. Storing this in Postgres rather than Redis
    keeps the app on a single, already-required dependency (see broadcast_processor's
    equivalent decision to drop Redis/RQ)."""

    __tablename__ = "phone_verifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    phone: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
