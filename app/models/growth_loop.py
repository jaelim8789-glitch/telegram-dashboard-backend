"""Autonomous Growth Loop — DB Model

연속적인 자율 성장 사이클:
Analyze → Generate → Send → Measure → Adjust → Repeat
"""

import uuid
from datetime import datetime

from sqlalchemy import String, Text, DateTime, Integer, Float, JSON, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AutonomousGrowthLoop(Base):
    __tablename__ = "autonomous_growth_loops"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    goal: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="idle")  # idle | running | paused | completed | failed
    current_cycle: Mapped[int] = mapped_column(Integer, default=0)

    # Strategy (JSON)
    strategy: Mapped[dict] = mapped_column(JSON, default=dict)

    # Metrics (JSON) — aggregated across all cycles
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)

    # Cycle log (JSON array)
    cycles: Mapped[list] = mapped_column(JSON, default=list)

    account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
