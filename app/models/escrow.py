"""Escrow models — buyer/seller transactions with milestone-based release."""

import uuid
from datetime import datetime

from sqlalchemy import String, Float, Integer, DateTime, Text, ForeignKey, JSON, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Escrow(Base):
    __tablename__ = "escrows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    chat_id: Mapped[str] = mapped_column(String(36), index=True)
    buyer_id: Mapped[str] = mapped_column(String(36), index=True)
    seller_id: Mapped[str] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(10), default="KRW")
    # pending, funded, in_progress, completed, disputed, refunded, cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    payment_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispute_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    milestones: Mapped[list["EscrowMilestone"]] = relationship("EscrowMilestone", back_populates="escrow", cascade="all, delete-orphan")
    messages: Mapped[list["EscrowMessage"]] = relationship("EscrowMessage", back_populates="escrow", cascade="all, delete-orphan")


class EscrowMilestone(Base):
    __tablename__ = "escrow_milestones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    escrow_id: Mapped[str] = mapped_column(String(36), ForeignKey("escrows.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount: Mapped[float] = mapped_column(Float)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    # pending, submitted, approved, rejected
    status: Mapped[str] = mapped_column(String(20), default="pending")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    escrow: Mapped["Escrow"] = relationship("Escrow", back_populates="milestones")


class EscrowMessage(Base):
    __tablename__ = "escrow_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    escrow_id: Mapped[str] = mapped_column(String(36), ForeignKey("escrows.id", ondelete="CASCADE"), index=True)
    sender_id: Mapped[str] = mapped_column(String(36))
    content: Mapped[str] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(String(20), default="text")  # text, system, evidence
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    escrow: Mapped["Escrow"] = relationship("Escrow", back_populates="messages")
