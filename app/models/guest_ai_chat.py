import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GuestAiChatLog(Base):
    """One row per guest (unauthenticated, no tenant) /ai chat exchange.

    Guest chat itself stays deliberately session-less (see ai_guest.py) --
    the client owns conversation state and nothing is tied to an account.
    This table exists purely so admins can review what anonymous visitors
    are asking (abuse/quality monitoring), not to reconstruct sessions.
    """

    __tablename__ = "guest_ai_chat_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ip: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    reply: Mapped[str] = mapped_column(Text, nullable=False)
    request_count_today: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
