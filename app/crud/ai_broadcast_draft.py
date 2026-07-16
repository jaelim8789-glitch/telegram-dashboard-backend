import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_broadcast_draft import AiBroadcastDraft


async def create_draft(
    db: AsyncSession, *, prompt: str, message: str, recommended_chat_ids: list[str], reasoning: str
) -> AiBroadcastDraft:
    row = AiBroadcastDraft(
        prompt=prompt,
        message=message,
        recommended_chat_ids_json=json.dumps(recommended_chat_ids, ensure_ascii=False),
        reasoning=reasoning,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def list_recent_drafts(db: AsyncSession, *, limit: int = 20) -> list[AiBroadcastDraft]:
    result = await db.execute(select(AiBroadcastDraft).order_by(AiBroadcastDraft.created_at.desc()).limit(limit))
    return list(result.scalars().all())
