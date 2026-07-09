import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, Time, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ReplyMacro(Base):
    """Canned-response / **답장매크로** model.
    
    Unlike AutoReplyRule (which reacts to incoming keyword-triggered messages),
    a ReplyMacro is a proactive "canned reply" that gets sent to one or more
    target chats on a fixed schedule or interval.
    
    Use cases:
    - Send a welcome message to a group every morning at 9 AM
    - Send a daily summary/notice at a specific time
    - Send an FAQ response to a specific chat every N hours
    """

    __tablename__ = "reply_macros"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Target: specific chat/group IDs (JSON array of strings)
    target_chats: Mapped[str] = mapped_column(Text, default="[]")

    # The message content to send
    message_content: Mapped[str] = mapped_column(Text)
    # Optional media path (image/file to attach)
    media_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Schedule type: "interval" (every N hours) or "fixed" (daily at specific time)
    schedule_type: Mapped[str] = mapped_column(String(20), default="interval")  # interval, fixed

    # For interval mode: send every N hours
    interval_hours: Mapped[int] = mapped_column(Integer, default=24)

    # For fixed mode: daily at this time (HH:MM)
    fixed_time: Mapped[str | None] = mapped_column(String(5), nullable=True)  # "09:00"

    # Daily limit: max sends per day across all targets
    max_sends_per_day: Mapped[int] = mapped_column(Integer, default=10)

    # Last sent timestamp (to enforce interval)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ReplyMacroLog(Base):
    __tablename__ = "reply_macro_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    macro_id: Mapped[str] = mapped_column(ForeignKey("reply_macros.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    target_chat_id: Mapped[str] = mapped_column(String(100))
    message_sent: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), index=True)  # success, failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)