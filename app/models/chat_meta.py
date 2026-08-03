"""Per-conversation CRM metadata: status, assignee, internal note.

Telegram chats themselves aren't stored in our DB (they're fetched live via
Telethon), so this table is the only place a (tenant, account, chat) triple
gets app-side state attached to it.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

CHAT_STATUSES = ("new", "in_progress", "done", "hold")


class ChatMeta(Base):
    __tablename__ = "chat_meta"
    __table_args__ = (
        UniqueConstraint("tenant_id", "account_id", "chat_id", name="uq_chat_meta_chat"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    chat_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="new", nullable=False)
    assignee_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
