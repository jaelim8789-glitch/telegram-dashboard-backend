"""Trust & Reputation models — trust scores and transaction reviews."""

import uuid
from datetime import datetime

from sqlalchemy import String, Float, Integer, DateTime, Text, ForeignKey, JSON, func
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class TrustProfile(Base):
    __tablename__ = "trust_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    telegram_user_id: Mapped[str] = mapped_column(String(36), index=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    trust_score: Mapped[float] = mapped_column(Float, default=0.0)
    total_transactions: Mapped[int] = mapped_column(Integer, default=0)
    successful_transactions: Mapped[int] = mapped_column(Integer, default=0)
    disputed_transactions: Mapped[int] = mapped_column(Integer, default=0)
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    avg_rating: Mapped[float] = mapped_column(Float, default=0.0)
    badges: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TransactionReview(Base):
    __tablename__ = "transaction_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    escrow_id: Mapped[str] = mapped_column(String(36), ForeignKey("escrows.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    reviewer_id: Mapped[str] = mapped_column(String(36), index=True)
    target_id: Mapped[str] = mapped_column(String(36), index=True)
    rating: Mapped[int] = mapped_column(Integer)  # 1-5
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # ["fast", "reliable", etc.]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
