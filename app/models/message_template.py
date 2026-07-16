import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MessageTemplate(Base):
    """메시지 템플릿 라이브러리"""
    
    __tablename__ = "message_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(50), default="general")  # general, promotion, notice, welcome
    content: Mapped[str] = mapped_column(Text)
    variables: Mapped[str] = mapped_column(Text, default="[]")  # JSON array of variable names
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class FollowUpRule(Base):
    """발송 후 자동 후속 메시지 (시퀀스)"""
    
    __tablename__ = "follow_up_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    
    # Trigger: when a broadcast is sent
    trigger_delay_hours: Mapped[int] = mapped_column(Integer, default=24)  # Send follow-up after N hours
    
    # Follow-up message
    message_content: Mapped[str] = mapped_column(Text)
    match_keyword: Mapped[str | None] = mapped_column(String(200), nullable=True)  # Only send if reply contains keyword
    
    # Limits
    max_sends_per_day: Mapped[int] = mapped_column(Integer, default=50)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

