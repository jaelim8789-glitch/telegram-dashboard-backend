import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AiOpsReport(Base):
    """A periodic, AI-generated ops summary (see app.services.ai_ops_service).

    Read-only for operators — generation is suggestion/report-only, no action
    is ever taken automatically from this table.
    """

    __tablename__ = "ai_ops_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    report: Mapped[str] = mapped_column(Text)
    anomalies_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
