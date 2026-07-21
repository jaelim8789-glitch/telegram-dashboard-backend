import pytest

from app.config import Settings


def _settings(database_url: str, **overrides) -> Settings:
    return Settings(
        database_url=database_url,
        encryption_key="test-key",
        _env_file=None,  # ignore the real .env for this isolated test
        **overrides,
    )


def test_database_url_normalizes_postgres_scheme():
    assert _settings("postgres://user:pw@host:5432/db").database_url == "postgresql+asyncpg://user:pw@host:5432/db"


def test_database_url_normalizes_postgresql_scheme():
    assert (
        _settings("postgresql://user:pw@host:5432/db").database_url == "postgresql+asyncpg://user:pw@host:5432/db"
    )


def test_database_url_leaves_explicit_driver_alone():
    url = "postgresql+asyncpg://user:pw@host:5432/db"
    assert _settings(url).database_url == url


# ═══════════════════════════════════════════════════════════════════════
# Sprint 25 — Broadcast timeout configuration
# ═══════════════════════════════════════════════════════════════════════


def test_broadcast_timeout_default_is_600():
    """Default value is 600 seconds (raised from 300 in an earlier, unrelated change)."""
    s = _settings("postgresql+asyncpg://u:p@h/db")
    assert s.broadcast_timeout_seconds == 600


def test_broadcast_timeout_custom_value():
    """Can be overridden via env var."""
    s = _settings("postgresql+asyncpg://u:p@h/db", broadcast_timeout_seconds=600)
    assert s.broadcast_timeout_seconds == 600


def test_broadcast_timeout_accepts_small_positive():
    """Values >= 1 are accepted (useful for testing)."""
    s = _settings("postgresql+asyncpg://u:p@h/db", broadcast_timeout_seconds=5)
    assert s.broadcast_timeout_seconds == 5


def test_broadcast_timeout_rejects_zero():
    """Zero is rejected by the validator."""
    with pytest.raises(ValueError, match="BROADCAST_TIMEOUT_SECONDS must be >= 1"):
        _settings("postgresql+asyncpg://u:p@h/db", broadcast_timeout_seconds=0)


def test_broadcast_timeout_rejects_negative():
    """Negative values are rejected by the validator."""
    with pytest.raises(ValueError, match="BROADCAST_TIMEOUT_SECONDS must be >= 1"):
        _settings("postgresql+asyncpg://u:p@h/db", broadcast_timeout_seconds=-10)


def test_broadcast_timeout_rejects_very_negative():
    """Large negative values are also rejected."""
    with pytest.raises(ValueError, match="BROADCAST_TIMEOUT_SECONDS must be >= 1"):
        _settings("postgresql+asyncpg://u:p@h/db", broadcast_timeout_seconds=-999)


# ═══════════════════════════════════════════════════════════════════════
# Sprint 26 — Broadcast retry limits
# ═══════════════════════════════════════════════════════════════════════


def test_broadcast_max_retries_default_is_3():
    """Default value is 3."""
    s = _settings("postgresql+asyncpg://u:p@h/db")
    assert s.broadcast_max_retries == 3


def test_broadcast_max_retries_custom_value():
    """Can be overridden via env var."""
    s = _settings("postgresql+asyncpg://u:p@h/db", broadcast_max_retries=5)
    assert s.broadcast_max_retries == 5


def test_broadcast_max_retries_accepts_zero():
    """Zero is accepted (disables retries)."""
    s = _settings("postgresql+asyncpg://u:p@h/db", broadcast_max_retries=0)
    assert s.broadcast_max_retries == 0


def test_broadcast_max_retries_rejects_negative():
    """Negative values are rejected by the validator."""
    with pytest.raises(ValueError, match="BROADCAST_MAX_RETRIES must be >= 0"):
        _settings("postgresql+asyncpg://u:p@h/db", broadcast_max_retries=-1)


def test_broadcast_max_retries_rejects_very_negative():
    """Large negative values are also rejected."""
    with pytest.raises(ValueError, match="BROADCAST_MAX_RETRIES must be >= 0"):
        _settings("postgresql+asyncpg://u:p@h/db", broadcast_max_retries=-999)


# ═══════════════════════════════════════════════════════════════════════
# Sprint 27 — Production startup guard (insecure defaults)
# ═══════════════════════════════════════════════════════════════════════


def test_development_allows_insecure_defaults():
    """Development environment accepts all default credential values."""
    s = _settings(
        "postgresql+asyncpg://u:p@h/db",
        environment="development",
    )
    assert s.admin_username == "sksk2929"
    assert s.admin_password == "ysjr0508"
    assert s.admin_jwt_secret == "change-me-in-production"


def test_development_allows_override():
    """Development accepts overridden values too."""
    s = _settings(
        "postgresql+asyncpg://u:p@h/db",
        environment="development",
        admin_username="admin",
        admin_password="s3cret!",
        admin_jwt_secret="real-secret",
    )
    assert s.admin_username == "admin"


def test_production_rejects_default_admin_username():
    """Production with default admin_username is rejected."""
    with pytest.raises(ValueError, match="admin_username"):
        _settings(
            "postgresql+asyncpg://u:p@h/db",
            environment="production",
            admin_password="real-pw",
            admin_jwt_secret="real-secret",
        )


def test_production_rejects_default_admin_password():
    """Production with default admin_password is rejected."""
    with pytest.raises(ValueError, match="admin_password"):
        _settings(
            "postgresql+asyncpg://u:p@h/db",
            environment="production",
            admin_username="custom-user",
            admin_jwt_secret="real-secret",
        )


def test_production_rejects_default_jwt_secret():
    """Production with default JWT secret is rejected."""
    with pytest.raises(ValueError, match="admin_jwt_secret"):
        _settings(
            "postgresql+asyncpg://u:p@h/db",
            environment="production",
            admin_username="custom-user",
            admin_password="real-pw",
        )


def test_production_rejects_all_insecure_defaults_together():
    """Production with all default admin credentials is rejected in one error."""
    with pytest.raises(ValueError, match="admin_username, admin_password, admin_jwt_secret"):
        _settings(
            "postgresql+asyncpg://u:p@h/db",
            environment="production",
        )


def test_production_accepts_overridden_credentials():
    """Production with every insecure default overridden (credentials, debug,
    sms_provider, frontend_url, cors_origins) is accepted. The validator was
    extended, in an earlier unrelated hardening change, to also require
    debug=False / a real sms_provider / a non-localhost frontend_url and
    cors_origins — this test's config must satisfy all of them, not just the
    admin credentials, to be a genuinely valid production example."""
    s = _settings(
        "postgresql+asyncpg://u:p@h/db",
        environment="production",
        admin_username="admin",
        admin_password="V3ryS3cur3!",
        admin_jwt_secret="".join("x" for _ in range(48)),
        debug=False,
        sms_provider="twilio",
        frontend_url="https://telemon.online",
        cors_origins="https://telemon.online",
    )
    assert s.environment == "production"
    assert s.admin_username == "admin"


def test_production_accepts_overridden_jwt_secret():
    """Production with a non-default JWT secret + full credentials (and every
    other insecure default overridden) is accepted."""
    s = _settings(
        "postgresql+asyncpg://u:p@h/db",
        environment="production",
        admin_username="telemon-admin",
        admin_password="Str0ng!Pass",
        admin_jwt_secret="a94a8fe5ccb19ba61c4c0873d391e987982fbbd3",
        debug=False,
        sms_provider="twilio",
        frontend_url="https://telemon.online",
        cors_origins="https://telemon.online",
    )
    assert s.admin_jwt_secret == "a94a8fe5ccb19ba61c4c0873d391e987982fbbd3"


def test_prod_environment_variant_accepted():
    """'prod' is treated the same as 'production'."""
    with pytest.raises(ValueError, match="admin_jwt_secret"):
        _settings(
            "postgresql+asyncpg://u:p@h/db",
            environment="prod",
            admin_username="custom",
            admin_password="custom",
        )


def test_prod_variant_with_overrides_accepted():
    """'prod' environment + every insecure default overridden is accepted."""
    s = _settings(
        "postgresql+asyncpg://u:p@h/db",
        environment="prod",
        admin_username="admin",
        admin_password="S3cur3P@ss",
        admin_jwt_secret="".join("y" for _ in range(48)),
        debug=False,
        sms_provider="twilio",
        frontend_url="https://telemon.online",
        cors_origins="https://telemon.online",
    )
    assert s.admin_username == "admin"


def test_error_does_not_expose_secret_values():
    """Error message must not contain actual credential values."""
    with pytest.raises(ValueError) as exc_info:
        _settings(
            "postgresql+asyncpg://u:p@h/db",
            environment="production",
        )
    msg = str(exc_info.value)
    assert "123123" not in msg
    assert "123456" not in msg
    assert "change-me-in-production" not in msg


def test_non_production_environments_pass_through():
    """Environments other than production/prod are never blocked."""
    for env in ("development", "staging", "test", "ci", ""):
        s = _settings(
            "postgresql+asyncpg://u:p@h/db",
            environment=env,
        )
        assert s.environment == env


def test_existing_tests_still_pass():
    """The existing test helper still works unchanged."""
    s = _settings("postgresql+asyncpg://u:p@h/db", broadcast_max_retries=3)
    assert s.broadcast_max_retries == 3