import os
import socket

# Must run before any `app.*` import: app/config.py and app/database.py build a
# Settings()/engine singleton at import time, so the DB URL has to be overridden in the
# environment first. Only DATABASE_URL is isolated — ENCRYPTION_KEY / ADMIN_* / etc. are
# fine to share with local dev since tests never write real data through them.
#
# Falls back to SQLite when no local Postgres is reachable (e.g. this dev box) so the
# suite can still run; CI/production dev environments with Postgres available keep using
# it unchanged since this is just a connectivity probe, not a preference.
def _postgres_reachable() -> bool:
    try:
        with socket.create_connection(("localhost", 5432), timeout=0.5):
            return True
    except OSError:
        return False


if "DATABASE_URL" not in os.environ:
    if _postgres_reachable():
        os.environ["DATABASE_URL"] = (
            "postgresql+asyncpg://telegram_dashboard:telegram_dashboard@localhost:5432/telegram_dashboard_test"
        )
    else:
        os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_telemon.db"

# Test suite runs in "development" env to bypass production guards in .env
if "ENVIRONMENT" not in os.environ:
    os.environ["ENVIRONMENT"] = "development"
os.environ["DEBUG"] = "true"
os.environ["SMS_PROVIDER"] = "console"
# Set required env vars with test-friendly defaults
if "ENCRYPTION_KEY" not in os.environ:
    # Must be a valid Fernet key: 32 url-safe base64-encoded bytes.
    os.environ["ENCRYPTION_KEY"] = "I62a0BiduGAdZjg9UH_vg3VuIEQMpe2AyDm2DfM2HlA="
if "TELEGRAM_API_ID" not in os.environ:
    os.environ["TELEGRAM_API_ID"] = "12345"
if "TELEGRAM_API_HASH" not in os.environ:
    os.environ["TELEGRAM_API_HASH"] = "test_hash_abcdef"
# Ensure channel-verification gate is disabled by default for unrelated tests
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_OFFICIAL_CHANNEL_ID"] = ""

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.scheduler.scheduler as scheduler_module
import app.services.auto_reply_service as auto_reply_service_module
import app.services.broadcast_processor as broadcast_processor_module
from app.api.deps import Identity, get_current_identity, require_api_key_or_admin
from app.config import settings
from app.database import Base, get_db
from app.main import app


@pytest_asyncio.fixture
async def db_session(monkeypatch):
    # A fresh engine per test (pytest-asyncio gives each test function its own event
    # loop by default): asyncpg connections are bound to the loop they were opened on,
    # so a longer-lived engine reused across tests breaks in confusing ways (the same
    # class of bug hit in the RQ worker on Windows).
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    # app.services.broadcast_processor and app.scheduler.scheduler each imported
    # `async_session_maker` by name at module-load time, so they hold their own binding
    # independent of app.database's — patch those bindings too, so code exercised via
    # those modules (e.g. process_broadcast) uses *this* test's engine instead of the
    # real module-level singleton (which points at this loop-reuse problem again).
    monkeypatch.setattr(broadcast_processor_module, "async_session_maker", session_maker)
    monkeypatch.setattr(scheduler_module, "async_session_maker", session_maker)
    monkeypatch.setattr(auto_reply_service_module, "async_session_maker", session_maker)

    async with session_maker() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db_session):
    """Auth bypassed — for tests about accounts/broadcast/groups/logs business logic,
    which isn't what they're testing. See `unauthenticated_client` for the auth checks
    themselves (login, API key issuance/validation, 401 rejection).

    Route handlers call get_current_identity directly (not just the router-level
    require_api_key_or_admin dependency) to resolve tenant scoping, so that also needs
    overriding here — otherwise every handler that touches identity.tenant_id/kind still
    401s even with require_api_key_or_admin bypassed. Defaults to an admin identity
    (cross-tenant, matches "auth bypassed"); a test that needs a specific tenant_id can
    re-override app.dependency_overrides[get_current_identity] after pulling in `client`.
    """
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_api_key_or_admin] = lambda: None
    app.dependency_overrides[get_current_identity] = lambda: Identity(kind="admin")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def unauthenticated_client(db_session):
    """The real require_api_key_or_admin dependency is left in place — use this for
    testing the auth mechanism itself. Also overrides get_db to use the test session
    so data committed in the test is visible to the endpoint."""
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


import pytest

import app.core.rate_limiter as _rate_limiter_module


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Reset in-memory rate-limiter state after each test.

    The ASGI test client always reports 127.0.0.1 as the client IP, so all
    send-code/verify-code calls across the full suite accumulate in the same
    rate-limit bucket and trigger 429s unrelated to the test under test.
    Production always sees real client IPs so this isolation is safe.
    """
    yield
    _rate_limiter_module.reset_rate_limit_for_ip("127.0.0.1")
