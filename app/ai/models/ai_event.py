"""
AI Event Models — event subscriptions and event logs for the AI Event Bus.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AiEventSubscription(Base):
    """AI Event Subscription — registered event handlers."""

    __tablename__ = "ai_event_subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    # tool.executed | workflow.completed | task.failed | schedule.triggered | custom.*
    handler_type: Mapped[str] = mapped_column(String(50), nullable=False, default="function")
    # function | webhook | plugin | workflow
    handler_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    # Python dotted path, URL, plugin name, or workflow ID
    filter_condition: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    # JSON filter: {"tenant_id": "...", "status": "failed"}
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    retry_on_failure: Mapped[bool] = mapped_column(Boolean, default=True)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    meta: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AiEventLog(Base):
    """AI Event Log — record of every event published on the bus."""

    __tablename__ = "ai_event_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    # system | tool | workflow | task | scheduler | plugin | api | user
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    correlation_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    handler_count: Mapped[int] = mapped_column(Integer, default=0)
    handler_success_count: Mapped[int] = mapped_column(Integer, default=0)
    handler_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)