from datetime import datetime, timezone

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import UserSession, hash_session_token, _session_expires_at


async def create_session(
    db: AsyncSession,
    *,
    user_id: str | None = None,
    tenant_id: str | None = None,
    api_key_id: str | None = None,
) -> tuple[str, UserSession]:
    """Create a new session. Returns (raw_token, session_row)."""
    raw_token = f"sx-{__import__('secrets').token_urlsafe(32)}"
    token_hash = hash_session_token(raw_token)
    session = UserSession(
        id=__import__('uuid').uuid4().hex[:36],
        token_hash=token_hash,
        user_id=user_id,
        tenant_id=tenant_id,
        api_key_id=api_key_id,
        expires_at=_session_expires_at(),
        last_used_at=datetime.now(timezone.utc),
    )
    db.add(session)
    await db.flush()
    return raw_token, session


async def get_session_by_token(db: AsyncSession, raw_token: str) -> UserSession | None:
    if not raw_token.startswith("sx-"):
        return None
    token_hash = hash_session_token(raw_token)
    result = await db.execute(
        select(UserSession).where(
            UserSession.token_hash == token_hash,
            UserSession.is_active == True,
            UserSession.expires_at > datetime.now(timezone.utc),
        )
    )
    return result.scalar_one_or_none()


async def touch_session(db: AsyncSession, session: UserSession) -> None:
    session.last_used_at = datetime.now(timezone.utc)
    await db.flush()


async def deactivate_session(db: AsyncSession, session: UserSession) -> None:
    session.is_active = False
    await db.flush()


async def deactivate_all_user_sessions(db: AsyncSession, user_id: str) -> None:
    await db.execute(
        delete(UserSession).where(
            UserSession.user_id == user_id,
            UserSession.is_active == True,
        )
    )
    await db.flush()


async def cleanup_expired_sessions(db: AsyncSession) -> int:
    result = await db.execute(
        delete(UserSession).where(UserSession.expires_at <= datetime.now(timezone.utc))
    )
    await db.flush()
    return result.rowcount
