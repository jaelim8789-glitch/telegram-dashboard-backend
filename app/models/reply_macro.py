import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ReplyMacro(Base):
    __tablename__ = "reply_macros"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    target_chats: Mapped[str] = mapped_column(Text, default="[]")
    message_content: Mapped[str] = mapped_column(Text)
    media_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # schedule_type: "interval", "fixed", "random_reply"
    schedule_type: Mapped[str] = mapped_column(String(20), default="interval")

    interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    fixed_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    max_sends_per_day: Mapped[int] = mapped_column(Integer, default=10)
    reply_to_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 중복 제외: 이미 답장한 대상 (JSON: [{chat_id, user_id}])
    used_targets: Mapped[str] = mapped_column(Text, default="[]")

    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ReplyMacroLog(Base):
    __tablename__ = "reply_macro_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    macro_id: Mapped[str] = mapped_column(ForeignKey("reply_macros.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    target_chat_id: Mapped[str] = mapped_column(String(100))
    replied_user_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    replied_msg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message_sent: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)