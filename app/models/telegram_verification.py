import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TelegramChannelVerification(Base):
    """One row per free-trial signup attempt that requires official-channel membership.

    The row's id IS the opaque token handed to the frontend and embedded in the bot
    deep link (t.me/<bot>?start=<id>) — there is no separate token column.

    Lifecycle: pending (created by /telegram-verify/start, no Telegram identity yet)
    -> linked (bot received /start <token>, telegram_user_id now known — this is the
    only step that can't be spoofed by the frontend, since it comes from Telegram's own
    servers to our bot) -> verified (server-side getChatMember confirmed active
    membership) -> consumed (spent to create exactly one trial; never reusable after).
    """

    __tablename__ = "telegram_channel_verifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, linked, verified
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    linked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
