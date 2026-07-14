"""Tests for session persistence (API key login, session token, logout)."""

import pytest

from app.crud import session as session_crud
from app.models.session import UserSession, generate_session_token, hash_session_token


@pytest.mark.asyncio
async def test_session_create_and_retrieve(db_session):
    """Create a session, retrieve by token, verify fields."""
    raw, session = await session_crud.create_session(
        db_session, user_id="test-user", tenant_id="test-tenant",
    )
    assert raw.startswith("sx-")
    assert session.is_active is True
    assert session.user_id == "test-user"
    assert session.tenant_id == "test-tenant"

    fetched = await session_crud.get_session_by_token(db_session, raw)
    assert fetched is not None
    assert fetched.id == session.id


@pytest.mark.asyncio
async def test_session_invalid_token_returns_none(db_session):
    """Wrong prefix or unknown hash returns None."""
    result = await session_crud.get_session_by_token(db_session, "invalid-token")
    assert result is None


@pytest.mark.asyncio
async def test_session_expired_is_rejected(db_session):
    """Expired sessions are not returned."""
    from datetime import datetime, timezone, timedelta
    import hashlib, secrets

    expired_token = f"sx-{secrets.token_urlsafe(32)}"
    expired_hash = hashlib.sha256(expired_token.encode()).hexdigest()
    session = UserSession(
        id="expired-id",
        token_hash=expired_hash,
        user_id="user",
        tenant_id="tenant",
        is_active=True,
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        last_used_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db_session.add(session)
    await db_session.flush()

    result = await session_crud.get_session_by_token(db_session, expired_token)
    assert result is None


@pytest.mark.asyncio
async def test_session_deactivate(db_session):
    """Deactivated sessions are not returned."""
    raw, session = await session_crud.create_session(db_session, user_id="user")
    await session_crud.deactivate_session(db_session, session)
    result = await session_crud.get_session_by_token(db_session, raw)
    assert result is None


@pytest.mark.asyncio
async def test_session_touch_updates_last_used(db_session):
    """touch_session updates last_used_at."""
    from datetime import datetime, timezone
    raw, session = await session_crud.create_session(db_session, user_id="user")
    old = session.last_used_at
    await session_crud.touch_session(db_session, session)
    assert session.last_used_at >= (old or datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_cleanup_expired_sessions(db_session):
    """cleanup_expired_sessions removes expired rows."""
    from datetime import datetime, timezone, timedelta
    import hashlib, secrets

    for i in range(3):
        t = f"sx-{secrets.token_urlsafe(32)}"
        h = hashlib.sha256(t.encode()).hexdigest()
        db_session.add(UserSession(
            id=f"cleanup-{i}", token_hash=h, user_id=f"u{i}",
            is_active=True,
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        ))
    await db_session.flush()
    deleted = await session_crud.cleanup_expired_sessions(db_session)
    assert deleted == 3


@pytest.mark.asyncio
async def test_deactivate_all_user_sessions(db_session):
    """All active sessions for a user are deactivated."""
    for i in range(2):
        await session_crud.create_session(db_session, user_id="user-a")
    await session_crud.create_session(db_session, user_id="user-b")
    await session_crud.deactivate_all_user_sessions(db_session, "user-a")

    from sqlalchemy import select
    from app.models.session import UserSession
    result = await db_session.execute(
        select(UserSession).where(UserSession.user_id == "user-a", UserSession.is_active == True)
    )
    assert result.scalar_one_or_none() is None
    # user-b session still active
    result = await db_session.execute(
        select(UserSession).where(UserSession.user_id == "user-b", UserSession.is_active == True)
    )
    assert result.scalar_one_or_none() is not None
