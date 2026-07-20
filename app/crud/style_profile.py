from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.style_profile import StyleProfile


async def get_style_profile(db: AsyncSession, style_profile_id: str) -> StyleProfile | None:
    return await db.get(StyleProfile, style_profile_id)


async def list_style_profiles(db: AsyncSession, account_id: str | None = None) -> list[StyleProfile]:
    query = select(StyleProfile).order_by(StyleProfile.created_at.desc())
    if account_id:
        query = query.where(StyleProfile.account_id == account_id)
    result = await db.execute(query)
    return list(result.scalars().all())
