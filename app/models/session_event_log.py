"""Session Event Log model — stores recent events per account."""

from datetime import datetime
from sqlalchemy import String, Text, Integer, DateTime, JSON, func
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class SessionEventLog(Base):
    __tablename__ = "session_event_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    event: Mapped[str] = mapped_column(String(50))
    health: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
