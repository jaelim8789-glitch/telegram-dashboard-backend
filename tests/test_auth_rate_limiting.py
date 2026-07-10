import pytest

from app.config import settings
from app.core.rate_limiter import check_rate_limit, reset_rate_limits


@pytest.mark.asyncio
async def test_admin_login_allows_below_threshold(unauthenticated_client):
    """Normal admin login attempts remain allowed below threshold."""
    reset_rate_limits()
    for _ in range(5):
        res = await unauthenticated_client.post(
            "/api/admin/login",
            json={"username": settings.admin_username, "password": settings.admin_password},
        )
        assert res.status_code == 200


@pytest.mark.asyncio
async def test_admin_login_rate_limited_after_excess(unauthenticated_client):
    """Repeated admin login attempts trigger rate limiting."""
    reset_rate_limits()

    # Exhaust the limit (10 attempts)
    for i in range(10):
        res = await unauthenticated_client.post(
            "/api/admin/login",
            json={"username": settings.admin_username, "password": "wrong-but-counts"},
        )
        # All should 401 (wrong password), never 429 yet
        assert res.status_code == 401, f"Attempt {i+1} unexpectedly returned {res.status_code}"

    # The 11th request should be rate-limited
    res = await unauthenticated_client.post(
        "/api/admin/login",
        json={"username": settings.admin_username, "password": settings.admin_password},
    )
    assert res.status_code == 429
    assert "Retry-After" in res.headers
    assert int(res.headers["Retry-After"]) > 0


@pytest.mark.asyncio
async def test_admin_login_429_response_has_expected_body(unauthenticated_client):
    """429 response body does not reveal credential validity."""
    reset_rate_limits()

    for _ in range(10):
        await unauthenticated_client.post(
            "/api/admin/login",
            json={"username": "anyone", "password": "anything"},
        )

    res = await unauthenticated_client.post(
        "/api/admin/login",
        json={"username": settings.admin_username, "password": settings.admin_password},
    )
    assert res.status_code == 429
    body = res.json()
    assert "detail" in body
    # The detail must not reveal whether the credentials would have been valid
    assert "올바르지 않습니다" not in body["detail"]


@pytest.mark.asyncio
async def test_api_key_login_allows_below_threshold(unauthenticated_client, monkeypatch):
    """Normal API key login attempts remain allowed below threshold."""
    from unittest.mock import AsyncMock

    captured: dict[str, str] = {}

    async def fake_send(phone: str, code: str) -> None:
        captured["code"] = code

    monkeypatch.setattr("app.api.auth.send_verification_sms", fake_send)
    await unauthenticated_client.post("/api/auth/send-code", json={"phone": "+821000000020"})
    verify_res = await unauthenticated_client.post(
        "/api/auth/verify-code", json={"phone": "+821000000020", "code": captured["code"]}
    )
    api_key = verify_res.json()["api_key"]

    for _ in range(5):
        res = await unauthenticated_client.post(
            "/api/auth/login-with-api-key",
            json={"api_key": api_key},
        )
        assert res.status_code == 200


@pytest.mark.asyncio
async def test_api_key_login_rate_limited_after_excess(unauthenticated_client, monkeypatch):
    """Repeated API key login attempts trigger rate limiting."""
    reset_rate_limits()
    from unittest.mock import AsyncMock

    monkeypatch.setattr("app.api.auth.send_verification_sms", AsyncMock(return_value=None))
    await unauthenticated_client.post("/api/auth/send-code", json={"phone": "+821000000021"})

    for i in range(20):
        res = await unauthenticated_client.post(
            "/api/auth/login-with-api-key",
            json={"api_key": "sk-invalid-key"},
        )
        assert res.status_code == 401, f"Attempt {i+1} unexpectedly returned {res.status_code}"

    res = await unauthenticated_client.post(
        "/api/auth/login-with-api-key",
        json={"api_key": "sk-any-key"},
    )
    assert res.status_code == 429
    assert "Retry-After" in res.headers


@pytest.mark.asyncio
async def test_api_key_login_429_does_not_expose_key_validity(unauthenticated_client):
    """429 response must not reveal whether API key was valid (without auth setup)."""
    reset_rate_limits()

    for _ in range(20):
        await unauthenticated_client.post(
            "/api/auth/login-with-api-key",
            json={"api_key": "sk-some-random-key"},
        )

    res = await unauthenticated_client.post(
        "/api/auth/login-with-api-key",
        json={"api_key": "sk-another-key"},
    )
    assert res.status_code == 429
    body = res.json()
    assert "detail" in body
    assert "올바르지 않습니다" not in body["detail"]
    assert "비활성화" not in body["detail"]


@pytest.mark.asyncio
async def test_admin_and_api_key_limits_are_independent(unauthenticated_client):
    """Admin login and API key login use separate counters."""
    reset_rate_limits()

    for _ in range(10):
        res = await unauthenticated_client.post(
            "/api/admin/login",
            json={"username": "any", "password": "any"},
        )
        assert res.status_code == 401

    # API key login should still work (different counter)
    res = await unauthenticated_client.post(
        "/api/auth/login-with-api-key",
        json={"api_key": "sk-test-not-real"},
    )
    # 401 is expected (invalid key), NOT 429
    assert res.status_code == 401, f"Expected 401, got {res.status_code}"


@pytest.mark.asyncio
async def test_successful_admin_login_contract_unaffected(unauthenticated_client):
    """Successful authentication response contract remains unchanged."""
    reset_rate_limits()
    res = await unauthenticated_client.post(
        "/api/admin/login",
        json={"username": settings.admin_username, "password": settings.admin_password},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["token_type"] == "bearer"
    assert len(body["access_token"]) > 0


@pytest.mark.asyncio
async def test_rate_limit_reset_works_for_tests(unauthenticated_client):
    """reset_rate_limits() clears state so subsequent tests are independent."""
    reset_rate_limits()

    for _ in range(10):
        await unauthenticated_client.post(
            "/api/admin/login",
            json={"username": "x", "password": "y"},
        )

    res = await unauthenticated_client.post(
        "/api/admin/login",
        json={"username": "x", "password": "y"},
    )
    assert res.status_code == 429

    reset_rate_limits()

    res = await unauthenticated_client.post(
        "/api/admin/login",
        json={"username": settings.admin_username, "password": settings.admin_password},
    )
    assert res.status_code == 200, "After reset, admin login should succeed"


@pytest.mark.asyncio
async def test_check_rate_limit_does_not_log_secrets(unauthenticated_client, monkeypatch):
    """Verify the rate limiter does not log raw credentials or API keys."""
    reset_rate_limits()
    captured_logs: list[str] = []

    class FakeLogger:
        def warning(self, msg, **kwargs):
            captured_logs.append(str(kwargs))

    monkeypatch.setattr("app.core.rate_limiter.logger", FakeLogger())

    for _ in range(11):
        await unauthenticated_client.post(
            "/api/admin/login",
            json={"username": "super-secret-admin", "password": "super-secret-password"},
        )

    for log in captured_logs:
        assert "super-secret-admin" not in log
        assert "super-secret-password" not in log
        assert "sk-" not in log


@pytest.mark.asyncio
async def test_rate_limiter_module_level_reset(unauthenticated_client):
    """Module-level reset_rate_limits() clears state completely."""
    from app.core.rate_limiter import _window

    reset_rate_limits()
    assert len(_window) == 0
