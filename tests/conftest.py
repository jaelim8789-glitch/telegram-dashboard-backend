import os
import asyncio

# Must run before any `app.*` import: app/config.py and app/database.py build a
# Settings()/engine singleton at import time, so the DB URL has to be overridden in the
# environment first. Only DATABASE_URL is isolated — ENCRYPTION_KEY / ADMIN_* / etc. are
# fine to share with local dev since tests never write real data through them.
_default_db_url = "postgresql+asyncpg://telegram_dashboard:telegram_dashboard@localhost:5432/telegram_dashboard_test"

# Detect if PostgreSQL is reachable; fall back to SQLite if not.
async def _probe_postgres() -> bool:
    try:
        from sqlalchemy.ext.asyncio import create_async_engine
        engine = create_async_engine(_default_db_url)
        async with engine.connect() as conn:
            await conn.execute(
                __import__("sqlalchemy").text("SELECT 1")
            )
        await engine.dispose()
        return True
    except Exception:
        return False

_probe_result = asyncio.run(_probe_postgres())
if not _probe_result:
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
else:
    os.environ.setdefault("DATABASE_URL", _default_db_url)

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.scheduler.scheduler as scheduler_module
import app.services.auto_reply_service as auto_reply_service_module
import app.services.broadcast_processor as broadcast_processor_module
from app.api.deps import require_api_key_or_admin
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
    themselves (login, API key issuance/validation, 401 rejection)."""
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_api_key_or_admin] = lambda: None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def unauthenticated_client(db_session):
    """The real require_api_key_or_admin dependency is left in place — use this for
    testing the auth mechanism itself."""
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
