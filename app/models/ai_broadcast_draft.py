import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AiBroadcastDraft(Base):
    """An audit trail entry for a POST /api/ai/generate-broadcast call.

    History-only — this never sends anything itself. Recorded for every call
    (even when the model's JSON reply degrades to plain text) so operators can
    review what AI drafted/recommended, matching the AutoReplySuggestion /
    AiOpsReport precedent of keeping a record of AI output.
    """

    __tablename__ = "ai_broadcast_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    prompt: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    recommended_chat_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    reasoning: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
