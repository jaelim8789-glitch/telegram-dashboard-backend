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


def test_broadcast_timeout_default_is_300():
    """Default value is 300 seconds."""
    s = _settings("postgresql+asyncpg://u:p@h/db")
    assert s.broadcast_timeout_seconds == 300


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