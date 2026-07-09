"""Delivery Message Log — per-recipient delivery record.

Stores operational evidence for every Telegram message delivery attempt
through the canonical delivery pipeline (app/services/delivery.py).

Each row represents one delivery attempt to one recipient.
Multiple rows for the same (source_type, source_id, recipient) indicate retries;
the row with status='success' is the authoritative final state.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MessageLog(Base):
    __tablename__ = "message_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    recipient: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(50), index=True, nullable=False)  # manual, broadcast, reply_macro, auto_reply, scheduled
    source_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)  # broadcast_id, macro_id, etc.

    # Delivery outcome
    status: Mapped[str] = mapped_column(String(30), index=True, nullable=False)  # success, flood_wait, network_error, session_expired, invalid_recipient, forbidden, banned, permanent_failure, internal_error
    success: Mapped[bool] = mapped_column(nullable=False, default=False)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Safe error — never contains secrets, session strings, or raw Telethon exceptions
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    message_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)