"""Database engine and session factory with production-safe connection pooling.

Connection hardening:
- ``pool_pre_ping``: verifies connections are alive before handing them out
  (prevents stale-connection errors after Render free-tier sleep or network blips).
- ``pool_size`` / ``max_overflow``: tuned for a single-worker uvicorn process.
- ``PoolTimeout`` is deliberately *not* set here — the caller (API route or scheduler)
  should handle the timeout and return a 503 / retry instead of crashing the process.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session.

    The session is automatically closed when the request finishes.
    If the database is unreachable, the ``SQLAlchemy`` error propagates to
    FastAPI's exception handler, which returns a 503 to the client.
    """
    async with async_session_maker() as session:
        yield session