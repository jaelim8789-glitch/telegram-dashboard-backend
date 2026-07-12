"""JoinQueue model — persisted queue of bulk-inspected links awaiting sequential join.

Each row represents one link queued for joining. The scheduler processes items
one at a time per account, respecting a configurable conservative join rate and
pausing on FloodWait errors.

Design inherits patterns from Broadcast (status machine, error recording) and
GroupSearchResult (join audit trail reuse).
"""

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class JoinQueueItem(Base):
    __tablename__ = "join_queue_items"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id = Column(String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    raw_link = Column(String, nullable=False)
    title = Column(String, nullable=True)
    chat_type = Column(String, nullable=True)  # group | megagroup | channel
    username = Column(String, nullable=True)
    chat_id = Column(String, nullable=True)

    # Status machine: queued → processing → success | failed | flood_wait
    status = Column(String, nullable=False, default="queued", index=True)
    error_message = Column(Text, nullable=True)
    flood_wait_until = Column(DateTime(timezone=True), nullable=True)

    # Ordering within the queue (per-account)
    position = Column(Integer, nullable=False, default=0)

    # Configurable per-queue-item: seconds to wait before processing this item
    # (allows operator to set a conservative rate across the queue)
    delay_before_seconds = Column(Float, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)

    # Relationship
    account = relationship("Account", backref="join_queue_items")

    def __repr__(self):
        return f"<JoinQueueItem {self.id} account={self.account_id} status={self.status} link={self.raw_link[:50]}>"


class JoinQueueConfig(Base):
    """Per-account configuration for the Smart Join Queue.

    Stored separately so the operator can tune the rate without touching
    individual queue items.
    """
    __tablename__ = "join_queue_configs"

    account_id = Column(String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    is_paused = Column(Boolean, nullable=False, default=False)
    joins_per_hour = Column(Integer, nullable=False, default=5)  # conservative default
    max_daily_joins = Column(Integer, nullable=False, default=20)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    account = relationship("Account", backref="join_queue_config")

    def __repr__(self):
        return f"<JoinQueueConfig account={self.account_id} paused={self.is_paused} rate={self.joins_per_hour}/h>"