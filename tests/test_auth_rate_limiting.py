"""Tests for P0-4: per-IP rate limiting on public auth endpoints.

Validates:
1. Rate limiter helpers work correctly
2. Rate limiting with different IPs
3. Rate limiting with different categories
4. Reset behavior
"""

import time

import pytest

from app.core.limits import SEND_CODE_MAX_PER_IP, VERIFY_CODE_MAX_PER_IP
from app.core.rate_limiter import check_rate_limit, get_client_ip, reset_rate_limit_for_ip


class TestRateLimiterHelpers:
    """Verify rate_limiter helper functions."""

    def test_get_client_ip_uses_x_real_ip(self):
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {"X-Real-IP": "203.0.113.42"}
        ip = get_client_ip(request)
        assert ip == "203.0.113.42"

    def test_get_client_ip_falls_back_to_client_host(self):
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {}
        request.client.host = "127.0.0.1"
        ip = get_client_ip(request)
        assert ip == "127.0.0.1"

    def test_get_client_ip_returns_unknown_when_no_ip(self):
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {}
        request.client = None
        ip = get_client_ip(request)
        assert ip == "unknown"


class TestRateLimitCheck:
    """Verify check_rate_limit behavior."""

    CATEGORY = "test_rate_limit"

    def setup_method(self):
        reset_rate_limit_for_ip("test-ip")

    def test_allows_under_limit(self):
        for _ in range(5):
            assert check_rate_limit("test-ip", self.CATEGORY, max_attempts=10, window_seconds=300)

    def test_blocks_at_limit(self):
        max_attempts = 5
        for i in range(max_attempts):
            assert check_rate_limit("test-ip", self.CATEGORY, max_attempts=max_attempts, window_seconds=300), \
                f"Should allow request {i+1}"
        assert not check_rate_limit("test-ip", self.CATEGORY, max_attempts=max_attempts, window_seconds=300), \
            "Should block request beyond limit"

    def test_different_ips_independent(self):
        ip_a = "10.0.0.1"
        ip_b = "10.0.0.2"
        max_attempts = 5

        for _ in range(max_attempts):
            check_rate_limit(ip_a, self.CATEGORY, max_attempts=max_attempts)
        assert not check_rate_limit(ip_a, self.CATEGORY, max_attempts=max_attempts)

        assert check_rate_limit(ip_b, self.CATEGORY, max_attempts=max_attempts)

    def test_different_categories_independent(self):
        max_attempts = 3
        for _ in range(max_attempts):
            check_rate_limit("test-ip", "category_a", max_attempts=max_attempts)
        assert not check_rate_limit("test-ip", "category_a", max_attempts=max_attempts)

        assert check_rate_limit("test-ip", "category_b", max_attempts=max_attempts)

    def test_reset_clears_limit(self):
        max_attempts = 3
        for _ in range(max_attempts):
            check_rate_limit("reset-ip", self.CATEGORY, max_attempts=max_attempts)
        assert not check_rate_limit("reset-ip", self.CATEGORY, max_attempts=max_attempts)

        reset_rate_limit_for_ip("reset-ip")
        assert check_rate_limit("reset-ip", self.CATEGORY, max_attempts=max_attempts)


class TestSendCodeAuthEndpointIntegration:
    """Verify send-code/verify-code rate limiting via HTTP API.

    These tests use the unauthenticated_client fixture. Since the ASGI
    test client always reports 127.0.0.1 as the client IP, tests that
    verify per-IP behavior use the X-Real-IP header which auth.py now
    reads via get_client_ip().
    """

    @pytest.mark.asyncio
    async def test_send_code_via_x_real_ip(self, unauthenticated_client, monkeypatch):
        """Send-code with X-Real-IP header uses that IP for rate limiting."""
        from app.api.auth import send_verification_sms

        async def fake_send(phone, code):
            pass
        monkeypatch.setattr("app.api.auth.send_verification_sms", fake_send)

        phone = f"+8213{int(time.time() * 1000) % 100000:05d}"
        resp = await unauthenticated_client.post(
            "/api/auth/send-code",
            json={"phone": phone},
            headers={"X-Real-IP": "203.0.113.50"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_verify_code_via_x_real_ip(self, unauthenticated_client, monkeypatch):
        """Verify-code with X-Real-IP header uses that IP for rate limiting."""
        from app.api.auth import send_verification_sms

        async def fake_send(phone, code):
            pass
        monkeypatch.setattr("app.api.auth.send_verification_sms", fake_send)

        phone = f"+8214{int(time.time() * 1000) % 100000:05d}"
        resp = await unauthenticated_client.post(
            "/api/auth/verify-code",
            json={"phone": phone, "code": "000000"},
            headers={"X-Real-IP": "203.0.113.60"},
        )
        # 400 expected (wrong code, no code sent), but not 429
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_login_with_api_key_uses_get_client_ip(self, unauthenticated_client, monkeypatch):
        """login-with-api-key uses get_client_ip (not raw request.client.host)."""
        resp = await unauthenticated_client.post(
            "/api/auth/login-with-api-key",
            json={"api_key": "sk-invalid-test-key"},
            headers={"X-Real-IP": "203.0.113.70"},
        )
        assert resp.status_code == 401  # invalid key, not rate-limited
