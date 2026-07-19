import uuid
from datetime import datetime

from sqlalchemy import String, Text, DateTime, ForeignKey, Integer, Boolean, Float, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Employee(Base):
    """AI Employee — 사용자가 생성하는 AI 직원."""
    __tablename__ = "employees"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(50), default="assistant")  # assistant, marketer, searcher, supporter
    personality: Mapped[str] = mapped_column(String(500), default="")
    expertise: Mapped[str] = mapped_column(String(500), default="")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    avatar: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # 성장 시스템
    level: Mapped[int] = mapped_column(Integer, default=1)
    exp: Mapped[int] = mapped_column(Integer, default=0)
    total_messages: Mapped[int] = mapped_column(Integer, default=0)

    # 마켓플레이스
    is_listed: Mapped[bool] = mapped_column(Boolean, default=False)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    rental_price: Mapped[float] = mapped_column(Float, default=0.0)
    original_owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class EmployeeMessage(Base):
    """Employee별 독립 채팅 메시지."""
    __tablename__ = "employee_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(20))  # user, assistant
    content: Mapped[str] = mapped_column(Text)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())