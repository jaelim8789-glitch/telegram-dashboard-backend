"""Database model for the IP/Session Blacklist."""

from datetime import datetime
from sqlalchemy import Column, String, DateTime, Text
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base
import uuid

class BlacklistedEntity(Base):
    """Stores blacklisted IPs or Device IDs."""
    __tablename__ = "blacklisted_entities"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_value = Column(String, nullable=False) # The IP address or Device ID string
    entity_type = Column(String, nullable=False) # 'IP' or 'DEVICE_ID'
    reason = Column(Text, default=None) # Reason for the block
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, default=None) # Optional expiry time
    blocked_by = Column(String, default="MANUAL") # Who added it: 'MANUAL', 'AUTO_RULE_X', etc.