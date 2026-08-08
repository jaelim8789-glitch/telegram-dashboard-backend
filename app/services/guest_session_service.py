"""Service for managing guest chat sessions."""
import uuid
from datetime import datetime, timedelta
from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.database import get_db
from app.models.guest_analytics import GuestAiChatLogExtended

SESSION_TIMEOUT_MINUTES = 30

async def get_or_create_session(db: AsyncSession, device_id: str) -> uuid.UUID:
    """
    Gets the current active session for a device_id or creates a new one
    if no recent session exists.
    """
    cutoff_time = datetime.utcnow() - timedelta(minutes=SESSION_TIMEOUT_MINUTES)
    
    # Find the most recent active session for this device
    stmt = (
        select(GuestAiChatLogExtended.session_id)
        .filter(
            and_(
                GuestAiChatLogExtended.device_id == device_id,
                GuestAiChatLogExtended.created_at > cutoff_time
            )
        )
        .order_by(GuestAiChatLogExtended.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    last_session_id = result.scalar_one_or_none()

    if last_session_id:
        return last_session_id
    else:
        # Create a new session ID
        return uuid.uuid4()

async def get_session_history(db: AsyncSession, session_id: uuid.UUID) -> list:
    """Retrieves the full chat history for a given session ID."""
    stmt = (
        select(GuestAiChatLogExtended)
        .filter(GuestAiChatLogExtended.session_id == session_id)
        .order_by(GuestAiChatLogExtended.created_at.asc())
    )
    result = await db.execute(stmt)
    return result.scalars().all()