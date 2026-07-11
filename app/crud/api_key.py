from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import generate_api_key
from app.models.api_key import APIKey


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def create_api_key(db: AsyncSession, name: str, tenant_id: str | None = None) -> APIKey:
    api_key = APIKey(key=generate_api_key(), name=name, tenant_id=tenant_id)
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)
    return api_key


async def list_api_keys(db: AsyncSession) -> list[APIKey]:
    result = await db.execute(select(APIKey).order_by(APIKey.created_at.desc()))
    return list(result.scalars().all())


async def get_api_key(db: AsyncSession, api_key_id: str) -> APIKey | None:
    return await db.get(APIKey, api_key_id)


async def get_by_key(db: AsyncSession, key: str) -> APIKey | None:
    result = await db.execute(select(APIKey).where(APIKey.key == key, APIKey.is_active == True))
    return result.scalar_one_or_none()


async def touch_last_used(db: AsyncSession, api_key: APIKey) -> None:
    api_key.last_used = _utcnow_naive()
    await db.commit()


async def revoke_api_key(db: AsyncSession, api_key: APIKey) -> None:
    """Soft-revoke an API key by marking it inactive. The row is preserved for audit."""
    api_key.is_active = False
    await db.commit()
