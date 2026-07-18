"""
AI Feature Models — SQLAlchemy models for all AI-related tables.

These models support:
- AI Chat (with Graphiti long-term memory)
- AI Reply Assistant (context-based reply suggestions)
- AI Broadcast Assistant (AI-generated broadcast messages)
- AI Operations Report (daily/weekly operational summaries)
- AI Usage System (credits/request limits per plan)
- Admin AI (audit logs, user AI history)
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AiChatLog(Base):
    """AI Chat 대화 로그 — 사용자별 모든 AI 대화 저장."""

    __tablename__ = "ai_chat_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user / assistant / system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    log_metadata: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True, default={})
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str | None] = mapped_column(String(50), default="deepseek-chat")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class AiReplyAssistantLog(Base):
    """AI Reply Assistant 로그 — 자동 답장 추천 기록."""

    __tablename__ = "ai_reply_assistant_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    chat_id: Mapped[str] = mapped_column(String(100), nullable=False)
    chat_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    incoming_message: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_reply: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    context_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    was_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class AiBroadcastAssistantLog(Base):
    """AI Broadcast Assistant 로그 — AI 발송 메시지 생성 기록."""

    __tablename__ = "ai_broadcast_assistant_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    purpose: Mapped[str] = mapped_column(String(500), nullable=False)
    target_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_message: Mapped[str] = mapped_column(Text, nullable=False)
    variant_a: Mapped[str | None] = mapped_column(Text, nullable=True)
    variant_b: Mapped[str | None] = mapped_column(Text, nullable=True)
    tone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    language: Mapped[str | None] = mapped_column(String(10), default="ko")
    was_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class AiOperationsReport(Base):
    """AI Operations Report — AI 운영 리포트."""

    __tablename__ = "ai_operations_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    report_type: Mapped[str] = mapped_column(String(50), nullable=False)  # daily / weekly / custom
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    sections: Mapped[list | None] = mapped_column(JSON, nullable=True, default=[])
    insights: Mapped[list | None] = mapped_column(JSON, nullable=True, default=[])
    recommendations: Mapped[list | None] = mapped_column(JSON, nullable=True, default=[])
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True, default={})
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class AiUsageRecord(Base):
    """AI Usage Record — AI 사용량 관리."""

    __tablename__ = "ai_usage_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    feature: Mapped[str] = mapped_column(String(50), index=True, nullable=False)  # chat / reply_assistant / broadcast_assistant / operations_report
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    requests_count: Mapped[int] = mapped_column(Integer, default=1)
    cost_credits: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class AiPlanLimit(Base):
    """AI Plan Limit — 플랜별 AI 제한 설정."""

    __tablename__ = "ai_plan_limits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    plan: Mapped[str] = mapped_column(String(50), nullable=False)  # free / starter / pro / enterprise
    feature: Mapped[str] = mapped_column(String(50), nullable=False)  # chat / reply_assistant / broadcast_assistant / operations_report
    max_requests_per_day: Mapped[int] = mapped_column(Integer, default=0)
    max_tokens_per_day: Mapped[int] = mapped_column(Integer, default=0)
    max_credits_per_month: Mapped[float] = mapped_column(Float, default=0.0)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())