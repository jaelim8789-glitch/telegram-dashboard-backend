import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Tenant(Base):
    """Multi-tenant / 요금제 기반 사용자 모델.
    
    Each tenant represents a customer with a specific plan and usage limits.
    This enables the SaaS monetization model where different plans have
    different feature limits.
    """

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100), default="")
    phone: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    
    # Plan
    plan: Mapped[str] = mapped_column(String(20), default="free")  # free, pro, team
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    trial_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    
    # Plan limits (denormalized for fast checks)
    max_accounts: Mapped[int] = mapped_column(Integer, default=1)
    max_auto_reply_rules: Mapped[int] = mapped_column(Integer, default=3)
    max_reply_macros: Mapped[int] = mapped_column(Integer, default=1)
    monthly_message_limit: Mapped[int] = mapped_column(Integer, default=100)
    monthly_auto_reply_limit: Mapped[int] = mapped_column(Integer, default=100)
    monthly_ai_chat_limit: Mapped[int] = mapped_column(Integer, default=20)
    cooldown_minimum_minutes: Mapped[int] = mapped_column(Integer, default=60)  # minimum cooldown

    # Extra AI Chat credits purchased with Telegram Stars, spent once the monthly
    # plan quota (monthly_ai_chat_limit) is exhausted.
    ai_chat_credit_balance: Mapped[int] = mapped_column(Integer, default=0)
    
    # Billing
    stripe_customer_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    subscription_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    subscription_status: Mapped[str] = mapped_column(String(20), default="inactive")  # active, inactive, pending, past_due, canceled
    payment_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)  # USDT payment reference (memo)
    
    # Billing cycle
    billing_period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    billing_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    
    # Telegram Stars wallet (for add-on purchases)
    stars_balance: Mapped[int] = mapped_column(Integer, default=0)
    
    # Features enabled
    can_broadcast: Mapped[bool] = mapped_column(Boolean, default=True)
    can_schedule: Mapped[bool] = mapped_column(Boolean, default=False)
    can_attach_images: Mapped[bool] = mapped_column(Boolean, default=False)
    can_export_data: Mapped[bool] = mapped_column(Boolean, default=False)
    can_use_api: Mapped[bool] = mapped_column(Boolean, default=True)
    
    # Referral
    referred_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    referral_code: Mapped[str] = mapped_column(String(20), unique=True, default=lambda: str(uuid.uuid4())[:8])
    referral_earnings: Mapped[int] = mapped_column(Integer, default=0)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    accounts: Mapped[list["Account"]] = relationship("Account", back_populates="tenant", foreign_keys="Account.tenant_id")


class PaymentRecord(Base):
    """Record of USDT payments received and processed."""
    
    __tablename__ = "payment_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tx_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)  # Tron transaction hash
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    from_address: Mapped[str] = mapped_column(String(100))
    amount_usdt: Mapped[int] = mapped_column(Integer, default=0)  # stored in cents (1500 = $15.00)
    plan: Mapped[str | None] = mapped_column(String(20), nullable=True)
    billing: Mapped[str | None] = mapped_column(String(10), nullable=True)  # monthly, annual
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, completed, unmatched, failed
    api_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    claimed: Mapped[bool] = mapped_column(Boolean, default=False)  # raw API key has been delivered to the user
    block_timestamp: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class UsageRecord(Base):
    """Track per-tenant usage for billing and rate limiting."""
    
    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(50), index=True)  # broadcast, auto_reply, reply_macro, api_call
    count: Mapped[int] = mapped_column(Integer, default=1)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class Lead(Base):
    """CRM / 리드 생성 - captured from auto-reply interactions."""
    
    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    
    # Lead info
    telegram_user_id: Mapped[str] = mapped_column(String(100))
    telegram_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    
    # Source
    source_chat_id: Mapped[str] = mapped_column(String(100))
    source_rule_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    
    # Engagement
    total_messages: Mapped[int] = mapped_column(Integer, default=0)
    last_interaction: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    
    # Tags (JSON array)
    tags: Mapped[str] = mapped_column(Text, default="[]")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AiChatMessage(Base):
    """A single turn (user or assistant) in a tenant's bot "AI Chat" conversation.

    Kept per (tenant_id, telegram_user_id) so ai_chat_service can rebuild recent
    context for DeepSeek and so history survives a bot process restart.
    """

    __tablename__ = "ai_chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    telegram_user_id: Mapped[str] = mapped_column(String(100), index=True)
    role: Mapped[str] = mapped_column(String(20))  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())