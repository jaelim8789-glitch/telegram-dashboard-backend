import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AutoReplyRule(Base):
    __tablename__ = "auto_reply_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    match_type: Mapped[str] = mapped_column(String(20))  # keyword, exact
    match_value: Mapped[str] = mapped_column(Text)

    reply_content: Mapped[str] = mapped_column(Text)

    cooldown_hours: Mapped[int] = mapped_column(Integer, default=1)
    max_replies_per_day: Mapped[int] = mapped_column(Integer, default=100)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AutoReplyLog(Base):
    __tablename__ = "auto_reply_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    rule_id: Mapped[str] = mapped_column(ForeignKey("auto_reply_rules.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[str] = mapped_column(String(100))
    user_id: Mapped[str] = mapped_column(String(100))
    user_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    trigger_message: Mapped[str] = mapped_column(Text)
    reply_sent: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), index=True)  # success, failed, rate_limited
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
