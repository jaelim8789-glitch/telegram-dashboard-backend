"""End-to-end regression test: Trial API-Key expiration bypass verification.

Verifies that an expired 24-hour Trial user cannot bypass `require_active_subscription`
by logging in with their Trial API Key (issued via verify-code/signup).

This test exercises the complete production authentication flow:
1. Signup via verify-code → receive Trial API Key
2. Login via login-with-api-key → receive Bearer JWT token
3. JWT resolves as identity.kind="user" (NOT "api_key") via _resolve_identity
4. /me endpoint reflects correct role and trial status
5. Protected routes return 403 for expired trials
6. Active trials work, paid users work, admin works
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.tenant import Tenant


# ── Helpers ──────────────────────────────────────────────────────────

async def _signup_get_key(client, phone: str, monkeypatch) -> str:
    """Full signup flow: send-code + verify-code → returns raw API key."""
    captured: dict[str, str] = {}

    async def fake_send(phone: str, code: str) -> None:
        captured["code"] = code

    monkeypatch.setattr("app.api.auth.send_verification_sms", fake_send)

    send_res = await client.post("/api/auth/send-code", json={"phone": phone})
    assert send_res.status_code == 200

    verify_res = await client.post(
        "/api/auth/verify-code",
        json={"phone": phone, "code": captured["code"]},
    )
    assert verify_res.status_code == 200
    return verify_res.json()["api_key"]


async def _login_with_key(client, api_key: str) -> str:
    """Login via login-with-api-key → return Bearer token."""
    login_res = await client.post("/api/auth/login-with-api-key", json={"api_key": api_key})
    assert login_res.status_code == 200
    return login_res.json()["access_token"]


async def _get_me(client, token: str) -> dict:
    """Call /auth/me with Bearer token."""
    me_res = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_res.status_code == 200
    return me_res.json()


PROTECTED_ENDPOINTS = [
    ("GET", "/api/accounts"),
    ("GET", "/api/accounts/summary"),
    ("GET", "/api/account-health"),
    ("GET", "/api/delivery-analytics/summary"),
    ("GET", "/api/logs"),
    ("GET", "/api/scheduler/upcoming"),
    ("GET", "/api/broadcast/recurring"),
]


# ── 1. ACTIVE trial API key: login succeeds, role="user", /me correct ─

@pytest.mark.asyncio
async def test_active_trial_api_key_works(unauthenticated_client, db_session, monkeypatch):
    """An active trial user who signs up and logs in via API key gets role="user"
    and correct subscription data from /me."""
    phone = "+821000000710"
    api_key = await _signup_get_key(unauthenticated_client, phone, monkeypatch)

    # Verify tenant was created with active trial
    tenant = (await db_session.execute(select(Tenant).where(Tenant.phone == phone))).scalar_one()
    assert tenant.plan == "free"
    assert tenant.subscription_status == "active"
    assert tenant.trial_expires_at is not None

    # Login with the trial API key
    token = await _login_with_key(unauthenticated_client, api_key)
    me = await _get_me(unauthenticated_client, token)

    # CRITICAL: role must be "user", NOT "api_key"
    assert me["role"] == "user", (
        f"Trial user login must resolve as role='user', got '{me['role']}'. "
        f"If role is 'api_key' here, the bypass exists."
    )
    assert me["subscription_status"] == "active"
    assert me["plan"] == "free"
    assert me["trial_expires_at"] is not None


# ── 2. EXPIRED trial API key: login works, but role="user" → blocked ─

@pytest.mark.asyncio
async def test_expired_trial_api_key_blocked_on_protected_routes(
    unauthenticated_client, db_session, monkeypatch
):
    """An expired trial user who signs up and logs in via API key gets role="user".
    Their trial API key login succeeds, but protected routes return 403."""
    phone = "+821000000711"
    api_key = await _signup_get_key(unauthenticated_client, phone, monkeypatch)

    # Manually expire the trial
    tenant = (await db_session.execute(select(Tenant).where(Tenant.phone == phone))).scalar_one()
    tenant.trial_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    tenant.subscription_status = "expired"
    await db_session.flush()

    # Login still works with expired trial
    token = await _login_with_key(unauthenticated_client, api_key)

    # /me still works and shows correct status
    me = await _get_me(unauthenticated_client, token)
    assert me["role"] == "user", (
        f"CRITICAL: Expired trial API key login must return role='user', got '{me['role']}'"
    )
    assert me["subscription_status"] == "expired"
    assert me["plan"] == "free"

    # Protected routes MUST return 403
    headers = {"Authorization": f"Bearer {token}"}
    for method, path in PROTECTED_ENDPOINTS:
        resp = await unauthenticated_client.request(method, path, headers=headers)
        assert resp.status_code == 403, (
            f"{method} {path} should be BLOCKED for expired trial, "
            f"got {resp.status_code}: {resp.text}. BYPASS CONFIRMED if status < 400."
        )
        assert "만료" in resp.json()["detail"], (
            f"Expected trial expiry message, got: {resp.json()['detail']}"
        )


# ── 3. Active trial API key can access protected routes ─────────────

@pytest.mark.asyncio
async def test_active_trial_api_key_accesses_protected_routes(
    unauthenticated_client, db_session, monkeypatch
):
    """An active trial user who signs up and logs in via API key can access
    protected functionality."""
    phone = "+821000000712"
    api_key = await _signup_get_key(unauthenticated_client, phone, monkeypatch)
    token = await _login_with_key(unauthenticated_client, api_key)

    headers = {"Authorization": f"Bearer {token}"}
    resp = await unauthenticated_client.get("/api/accounts", headers=headers)
    assert resp.status_code == 200, (
        f"Active trial must access accounts, got {resp.status_code}"
    )


# ── 4. Expired trial /billing and /auth/me still accessible ──────────

@pytest.mark.asyncio
async def test_expired_trial_api_key_still_accesses_auth_and_billing(
    unauthenticated_client, db_session, monkeypatch
):
    """Expired trial users must still be able to access /me and /billing."""
    phone = "+821000000713"
    api_key = await _signup_get_key(unauthenticated_client, phone, monkeypatch)

    tenant = (await db_session.execute(select(Tenant).where(Tenant.phone == phone))).scalar_one()
    tenant.trial_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    tenant.subscription_status = "expired"
    await db_session.flush()

    token = await _login_with_key(unauthenticated_client, api_key)
    headers = {"Authorization": f"Bearer {token}"}

    # /me must work
    me_resp = await unauthenticated_client.get("/api/auth/me", headers=headers)
    assert me_resp.status_code == 200

    # Billing status must work
    billing_resp = await unauthenticated_client.get(
        f"/api/billing/subscription/{tenant.id}", headers=headers
    )
    assert billing_resp.status_code == 200


# ── 5. Paid user API key works normally ──────────────────────────────

@pytest.mark.asyncio
async def test_paid_user_api_key_works(unauthenticated_client, db_session, monkeypatch):
    """A paid (pro) user who signs up and logs in via API key has full access."""
    phone = "+821000000714"
    api_key = await _signup_get_key(unauthenticated_client, phone, monkeypatch)

    # Upgrade tenant to pro
    tenant = (await db_session.execute(select(Tenant).where(Tenant.phone == phone))).scalar_one()
    tenant.plan = "pro"
    tenant.subscription_status = "active"
    tenant.trial_expires_at = None
    await db_session.flush()

    token = await _login_with_key(unauthenticated_client, api_key)
    me = await _get_me(unauthenticated_client, token)
    assert me["role"] == "user"
    assert me["plan"] == "pro"
    assert me["subscription_status"] == "active"

    headers = {"Authorization": f"Bearer {token}"}
    resp = await unauthenticated_client.get("/api/accounts", headers=headers)
    assert resp.status_code == 200, "Paid user must have full access"


# ── 6. Admin access is unaffected ──────────────────────────────────

@pytest.mark.asyncio
async def test_admin_unaffected_by_trial_enforcement(client):
    """Admin identity always has full access regardless of trial enforcement."""
    resp = await client.get("/api/accounts")
    assert resp.status_code == 200


# ── 7. Explicit verify: /me returns role="user" not "api_key" ───────

@pytest.mark.asyncio
async def test_verify_role_is_user_after_api_key_login(
    unauthenticated_client, monkeypatch
):
    """Confirm that login-with-api-key creates a JWT that _resolve_identity
    resolves as kind='user', not kind='api_key'. The role in /me must be 'user'."""
    phone = "+821000000715"
    api_key = await _signup_get_key(unauthenticated_client, phone, monkeypatch)
    token = await _login_with_key(unauthenticated_client, api_key)
    me = await _get_me(unauthenticated_client, token)

    assert me["role"] == "user", (
        f"CRITICAL: login-with-api-key must result in /me role='user', "
        f"not '{me['role']}'. If role is 'api_key', the bypass exists because "
        f"require_active_subscription exempts kind='api_key' on line 115 of deps.py."
    )


# ── 8. X-API-Key header with user-issued key does NOT match ─────────

@pytest.mark.asyncio
async def test_user_api_key_rejected_via_x_api_key_header(
    unauthenticated_client, db_session, monkeypatch
):
    """A user-issued trial API key passed as X-API-Key header should NOT match
    the APIKey table (user keys are in User.api_key_hash, not APIKey.key).
    It must return 401, not resolve as kind='api_key'."""
    phone = "+821000000716"
    api_key = await _signup_get_key(unauthenticated_client, phone, monkeypatch)

    # Pass the user's trial key as X-API-Key header
    resp = await unauthenticated_client.get(
        "/api/accounts", headers={"X-API-Key": api_key}
    )
    assert resp.status_code == 401, (
        f"User-issued API key as X-API-Key must return 401 (not in APIKey table), "
        f"got {resp.status_code}. If 200, user keys leak into APIKey identity path."
    )
