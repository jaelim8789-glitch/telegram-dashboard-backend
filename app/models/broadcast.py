import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy import Boolean as SA_Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    media_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    recipients: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # Null = send immediately. Set = held until this time.
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # ── Recurring broadcast fields ──────────────────────────────────
    # Non-null = this broadcast is a recurring parent.
    # Allowed values: 30, 60, 120, 180, 360, 720, 1440
    recurring_interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # When the recurring broadcast was cancelled (null = still active / never cancelled).
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    # ISO 8601 of the next scheduled occurrence for recurring broadcasts.
    next_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    # For child broadcasts created by recurring execution: points to the parent.
    parent_broadcast_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("broadcasts.id", ondelete="SET NULL"), nullable=True, default=None, index=True
    )
    # Whether this recurring parent is paused (keeps schedule but doesn't execute).
    is_recurring_paused: Mapped[bool] = mapped_column(SA_Boolean, default=False, server_default="0")
    # Delivery mode: "normal" (1분 간격), "cycle" (N분마다 라운드로빈), "bulk" (즉시 전체전송), "reply" (답장형 발송)
    delivery_mode: Mapped[str] = mapped_column(String(20), default="normal", server_default="normal")
    # When delivery_mode is "reply", the specific message ID to reply to (null = auto-fetch latest)
    reply_to_msg_id: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # Per-recipient pacing override in seconds for delivery_mode "normal" (null = default 60s cooldown pacing).
    delay_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)

    # ── Campaign linkage ────────────────────────────────────────────
    # Links this broadcast to a campaign for aggregation/grouping.
    campaign_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, default=None, index=True
    )

    # ── Send-to-Group fields ────────────────────────────────────────
    # When set, these group chat IDs are resolved to member/chat IDs at dispatch time
    # instead of using `recipients` directly. Recipients in this mode are the resolved
    # member list from all specified groups.
    group_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, default=None)
    # Whether group resolution has been performed. Set to True after resolution.
    groups_resolved: Mapped[bool] = mapped_column(SA_Boolean, default=False, server_default="0")

    # ── Inline keyboard buttons ─────────────────────────────────────
    # JSON array: [{"label": "...", "url": "..."}, ...]
    # These are rendered as URL inline buttons on the Telegram message.
    inline_buttons: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True, default=None)

    # ── Multi-account distribution ──────────────────────────────────
    # Non-null = this broadcast is one account's slice of a group list that was
    # split across the tenant's active accounts (see app/services/broadcast_distribution.py).
    # All slices of the same logical request share this id.
    distribution_batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True, default=None)