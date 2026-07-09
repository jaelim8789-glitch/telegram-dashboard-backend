import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RecurringSchedule(Base):
    __tablename__ = "recurring_schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    media_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)  # 30, 60, 120, 180, 360, 720, 1440
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    total_sends: Mapped[int] = mapped_column(Integer, default=0)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
