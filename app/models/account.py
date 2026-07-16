import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id", ondelete="SET NULL"), index=True, nullable=True)
    phone: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="inactive")  # active, inactive, banned
    today_sent: Mapped[int] = mapped_column(Integer, default=0)
    group_count: Mapped[int] = mapped_column(Integer, default=0)
    last_activity: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Fernet ciphertext of a Telethon StringSession — never store or return the raw session.
    # Encrypt/decrypt only via app.core.crypto.
    session_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Master switch for the auto-reply listener (app/services/auto_reply_service.py).
    # Individual AutoReplyRule.is_active flags only matter while this is also True.
    auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Opt-in (default off): when no AutoReplyRule matches, draft an AI reply
    # suggestion for operator review instead of doing nothing. Never sent
    # automatically — see app.services.ai_reply_service.
    ai_fallback_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Health tracking fields — updated by the delivery pipeline and health scanner
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    health_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationship
    tenant: Mapped["Tenant | None"] = relationship("Tenant", back_populates="accounts")