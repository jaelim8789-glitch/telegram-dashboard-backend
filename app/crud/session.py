from datetime import datetime, timezone

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import UserSession, hash_session_token, hash_session_binding, _session_expires_at


async def create_session(
    db: AsyncSession,
    *,
    user_id: str | None = None,
    tenant_id: str | None = None,
    api_key_id: str | None = None,
    user_agent: str | None = None,
    client_ip: str | None = None,
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
        user_agent_hash=hash_session_binding(user_agent) if user_agent else None,
        client_ip_hash=hash_session_binding(client_ip) if client_ip else None,
    )
    db.add(session)
    await db.flush()
    return raw_token, session


async def get_session_by_token(
    db: AsyncSession,
    raw_token: str,
    *,
    user_agent: str | None = None,
    client_ip: str | None = None,
) -> UserSession | None:
    """Look up a live session by raw token.

    When the caller supplies the request's User-Agent and/or client IP, the
    stored hashes are compared against them. A mismatch sets
    ``requires_reauth = True`` (persisted) — a SOFT signal, not a hard block:
    the session is still returned so the endpoint can surface a re-login
    prompt instead of silently failing. Sessions created before this feature
    have no stored hash, so they are never flagged.
    """
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
    session = result.scalar_one_or_none()
    if session is None:
        return None

    if session.user_agent_hash is not None and user_agent is not None:
        if session.user_agent_hash != hash_session_binding(user_agent):
            session.requires_reauth = True
    if session.client_ip_hash is not None and client_ip is not None:
        if session.client_ip_hash != hash_session_binding(client_ip):
            session.requires_reauth = True
    if session.requires_reauth:
        await db.flush()

    return session


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
