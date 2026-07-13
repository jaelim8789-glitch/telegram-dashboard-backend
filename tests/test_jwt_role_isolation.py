"""Focused security tests for the JWT/identity boundary between admin and
user-session tokens (app/core/security.py + app/api/deps.py._resolve_identity).

Both token kinds are signed with the same secret (settings.admin_jwt_secret)
by design — there's only one operator, just two login paths — so the ONLY
thing standing between a user token and admin-level access is the `sub`
claim being interpreted correctly. These tests pin that boundary directly,
independent of any specific issuance flow (free-trial, admin manual-issue,
bot self-service, etc.), so a regression here fails fast and close to the
actual mechanism rather than only via a downstream E2E symptom.
"""

import pytest

from app.api.deps import _resolve_identity
from app.core.security import (
    create_access_token,
    create_user_access_token,
    decode_access_token,
    decode_user_id_from_token,
)
from app.models.user import User


def test_admin_token_has_admin_subject():
    token = create_access_token()
    assert decode_access_token(token) is True


def test_user_token_never_satisfies_admin_subject():
    """A user-session token must never decode_access_token()==True — that's
    the sole gate _resolve_identity uses to grant Identity(kind="admin")."""
    user_token = create_user_access_token("some-user-id")
    assert decode_access_token(user_token) is False


def test_user_token_id_extraction_roundtrips():
    user_token = create_user_access_token("user-abc-123")
    assert decode_user_id_from_token(user_token) == "user-abc-123"


def test_admin_token_does_not_extract_as_user_id():
    admin_token = create_access_token()
    assert decode_user_id_from_token(admin_token) is None


@pytest.mark.asyncio
async def test_resolve_identity_user_token_yields_user_kind_not_admin(db_session):
    """End-to-end through the real dependency resolver (not a bypass fixture):
    a user token for an existing, active User must resolve to kind="user",
    never kind="admin", regardless of how many other identities exist."""
    user = User(phone="+821099995000")
    db_session.add(user)
    await db_session.flush()
    await db_session.commit()

    user_token = create_user_access_token(user.id)
    identity = await _resolve_identity(
        x_api_key=None,
        authorization=f"Bearer {user_token}",
        db=db_session,
    )

    assert identity is not None
    assert identity.kind == "user"
    assert identity.kind != "admin"
    assert identity.user is not None
    assert identity.user.id == user.id


@pytest.mark.asyncio
async def test_resolve_identity_admin_token_yields_admin_kind(db_session):
    """Sanity check the other direction so this file actually distinguishes
    the two paths rather than just asserting user != admin everywhere."""
    admin_token = create_access_token()
    identity = await _resolve_identity(
        x_api_key=None,
        authorization=f"Bearer {admin_token}",
        db=db_session,
    )

    assert identity is not None
    assert identity.kind == "admin"


@pytest.mark.asyncio
async def test_resolve_identity_inactive_user_token_rejected(db_session):
    """An inactive user's token must not resolve to any identity — inactive
    status is not itself a privilege change, but confirms the same lookup
    path doesn't silently upgrade a rejected user to some other kind."""
    user = User(phone="+821099995001", is_active=False)
    db_session.add(user)
    await db_session.flush()
    await db_session.commit()

    user_token = create_user_access_token(user.id)
    identity = await _resolve_identity(
        x_api_key=None,
        authorization=f"Bearer {user_token}",
        db=db_session,
    )

    assert identity is None
