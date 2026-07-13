from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.guide_hub import GuideHubMessage


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def get_latest(db: AsyncSession) -> GuideHubMessage | None:
    """The single tracked guide-hub message, if one has ever been published."""
    result = await db.execute(
        select(GuideHubMessage).order_by(GuideHubMessage.created_at.desc()).limit(1)
    )
    return result.scalars().first()


async def upsert(db: AsyncSession, chat_id: str, message_id: int) -> GuideHubMessage:
    """Record the chat/message id of the currently-published guide hub.

    Updates the existing tracked row in place (so a second publish call edits
    the same message) rather than accumulating one row per publish.
    """
    row = await get_latest(db)
    if row is None:
        row = GuideHubMessage(chat_id=chat_id, message_id=message_id)
        db.add(row)
    else:
        row.chat_id = chat_id
        row.message_id = message_id
        row.updated_at = utcnow_naive()
    await db.commit()
    await db.refresh(row)
    return row
