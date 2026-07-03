from app.config import Settings


def _settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        encryption_key="test-key",
        _env_file=None,  # ignore the real .env for this isolated test
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
