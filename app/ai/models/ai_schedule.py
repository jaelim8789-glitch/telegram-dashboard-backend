"""
AI Schedule Models — scheduled job definitions and execution records.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AiScheduleDefinition(Base):
    """AI Schedule Definition — recurring or one-time scheduled AI jobs."""

    __tablename__ = "ai_schedule_definitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # tool_call | workflow_run | task_enqueue | event_emit | plugin_action | api_call
    action_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # {"tool_name": "...", "arguments": {...}} or {"workflow_id": "...", "input": {...}}
    schedule_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # interval | cron | once
    interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cron_expression: Mapped[str | None] = mapped_column(String(100), nullable=True)
    run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # for "once" type
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Seoul")
    max_executions: Mapped[int] = mapped_column(Integer, default=0)  # 0 = unlimited
    current_executions: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    meta: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AiScheduleExecution(Base):
    """AI Schedule Execution — record of a scheduled job execution."""

    __tablename__ = "ai_schedule_executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    schedule_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # pending | running | completed | failed | skipped
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    triggered_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)