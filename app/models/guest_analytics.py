"""Database models for Guest AI Chat Analytics."""

from datetime import datetime
from sqlalchemy import Column, String, DateTime, Integer, Boolean, Text, ForeignKey, Float
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base
from app.database import Base
import uuid

class GuestAiChatLogExtended(Base):
    """
    Extended log for Guest AI Chat interactions with session and analysis data.
    This extends the basic logging in GuestAiChatLog (if it exists separately) or replaces it for advanced analytics.
    """
    __tablename__ = "guest_ai_chat_logs_extended"

    # Primary Key
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Original fields from GuestAiChatLog
    ip = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    reply = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    thumbs_up = Column(Boolean, default=None) # Assuming this exists

    # New fields for advanced analytics
    device_id = Column(String, nullable=False) # From _get_guest_device_id
    session_id = Column(UUID(as_uuid=True), nullable=False) # Unique identifier for a session
    turn_number = Column(Integer, default=1) # Turn number within the session
    confidence = Column(String, default=None) # From extract_confidence
    primary_category = Column(String, default=None) # e.g., 'Technical Inquiry', 'Sales'
    secondary_category = Column(String, default=None) # e.g., 'Account Setup' under 'Technical Inquiry'
    classification_confidence = Column(Float, default=None) # Confidence of the AI classifier
    converted_user_id = Column(UUID(as_uuid=True), default=None) # FK to User table if converted later
    conversion_tracked = Column(Boolean, default=False) # Flag if conversion attempt was made