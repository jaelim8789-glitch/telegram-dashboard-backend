import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ContentCalendarSetting(Base):
    __tablename__ = "content_calendar_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    daily_count: Mapped[int] = mapped_column(Integer, default=1)
    content_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    tone: Mapped[str] = mapped_column(String(20), default="short")
    group_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Seoul")
    send_hour: Mapped[int] = mapped_column(Integer, default=10)
    last_generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_generate_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
