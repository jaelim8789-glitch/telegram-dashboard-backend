import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TokenBalance(Base):
    """사용자별 토큰 잔액 및 누적 획득량."""
    __tablename__ = "token_balances"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False)
    balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lifetime_earned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class StreakRecord(Base):
    """사용자별 출석 체인 기록."""
    __tablename__ = "streak_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False)
    current_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    longest_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_checkin_date: Mapped[str | None] = mapped_column(String(10), nullable=True)  # YYYY-MM-DD
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class TokenTransaction(Base):
    """토큰 변동 내역 (차감/획득)."""
    __tablename__ = "token_transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # 양수=획득, 음수=차감
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(100), nullable=False)  # "checkin", "quest", "discovery", "ai_cost", "admin_topup"
    reference_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 연관된 퀘스트/발송 ID
    memo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
