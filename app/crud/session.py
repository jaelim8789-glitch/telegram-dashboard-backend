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
    """Create a new session. Returns (raw_token, session_row).

    Deactivates the user's prior sessions first — every retry of an
    abandoned/failed login flow (closed tab, network hiccup, etc.) used to
    call this and pile up a new row without ever cleaning up the old ones,
    so a single confused login attempt could leave a user with a dozen
    "duplicate logins" that were invisible anywhere and never expired.
    """
    if user_id:
        await deactivate_all_user_sessions(db, user_id)

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
    """Called on every request authenticated via X-Session-Token.

    Also slides expires_at forward, so an actively-used session never hits
    the 30-day wall — only a session that's genuinely gone unused for
    SESSION_EXPIRE_DAYS gets reaped by cleanup_expired_sessions(). Without
    this, "stay logged in until I log out" wasn't actually true: a daily
    user still got silently kicked out on day 30.
    """
    session.last_used_at = datetime.now(timezone.utc)
    session.expires_at = _session_expires_at()
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
