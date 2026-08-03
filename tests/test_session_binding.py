"""Tests for session device binding (User-Agent + client IP).

Sessions store SHA-256 hashes of the User-Agent and client IP they were
created with. get_session_by_token compares the current request's UA/IP
against those hashes and, on mismatch, persists requires_reauth=True — a
soft signal (the session is still returned) so the frontend can prompt a
re-login instead of silently accepting a stolen token.
"""

import pytest

from app.crud import session as session_crud
from app.models.session import hash_session_binding


@pytest.mark.asyncio
async def test_create_session_stores_hashes(db_session):
    """create_session with user_agent/client_ip stores SHA-256 hex digests."""
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    ip = "203.0.113.7"
    raw, session = await session_crud.create_session(
        db_session, user_id="u", tenant_id="t", user_agent=ua, client_ip=ip,
    )
    assert session.user_agent_hash == hash_session_binding(ua)
    assert session.client_ip_hash == hash_session_binding(ip)
    assert len(session.user_agent_hash) == 64
    assert len(session.client_ip_hash) == 64
    assert session.user_agent_hash != ua  # never stored raw


@pytest.mark.asyncio
async def test_get_session_matching_binding(db_session):
    """Same UA + IP → requires_reauth stays False."""
    raw, _ = await session_crud.create_session(
        db_session, user_id="u", tenant_id="t",
        user_agent="UA-A", client_ip="10.0.0.1",
    )
    fetched = await session_crud.get_session_by_token(
        db_session, raw, user_agent="UA-A", client_ip="10.0.0.1",
    )
    assert fetched is not None
    assert fetched.requires_reauth is False


@pytest.mark.asyncio
async def test_get_session_mismatched_ua(db_session):
    """Different UA → requires_reauth True, session still returned."""
    raw, _ = await session_crud.create_session(
        db_session, user_id="u", tenant_id="t",
        user_agent="UA-A", client_ip="10.0.0.1",
    )
    fetched = await session_crud.get_session_by_token(
        db_session, raw, user_agent="UA-B", client_ip="10.0.0.1",
    )
    assert fetched is not None
    assert fetched.requires_reauth is True


@pytest.mark.asyncio
async def test_get_session_mismatched_ip(db_session):
    """Different IP → requires_reauth True, session still returned."""
    raw, _ = await session_crud.create_session(
        db_session, user_id="u", tenant_id="t",
        user_agent="UA-A", client_ip="10.0.0.1",
    )
    fetched = await session_crud.get_session_by_token(
        db_session, raw, user_agent="UA-A", client_ip="10.0.0.2",
    )
    assert fetched is not None
    assert fetched.requires_reauth is True


@pytest.mark.asyncio
async def test_get_session_no_binding_never_flags(db_session):
    """Sessions created without binding data (or callers that don't supply
    UA/IP) are never flagged — legacy sessions keep working unchanged."""
    raw, _ = await session_crud.create_session(db_session, user_id="u", tenant_id="t")
    fetched = await session_crud.get_session_by_token(db_session, raw)
    assert fetched is not None
    assert fetched.requires_reauth is False
