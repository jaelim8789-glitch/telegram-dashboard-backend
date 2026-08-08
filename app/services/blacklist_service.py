"""Service for managing the IP/Session blacklist."""
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.blacklist import BlacklistedEntity

async def is_blacklisted(db: AsyncSession, entity_value: str, entity_type: str) -> bool:
    """Checks if an IP or Device ID is blacklisted."""
    stmt = select(BlacklistedEntity).filter(
        (BlacklistedEntity.entity_value == entity_value) &
        (BlacklistedEntity.entity_type == entity_type) &
        ((BlacklistedEntity.expires_at.is_(None)) | (BlacklistedEntity.expires_at > datetime.utcnow()))
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None

async def add_to_blacklist(db: AsyncSession, entity_value: str, entity_type: str, reason: str, blocked_by: str = "MANUAL"):
    """Adds an IP or Device ID to the blacklist."""
    blacklist_entry = BlacklistedEntity(
        entity_value=entity_value,
        entity_type=entity_type,
        reason=reason,
        blocked_by=blocked_by
    )
    db.add(blacklist_entry)
    await db.commit()
    await db.refresh(blacklist_entry)
    return blacklist_entry