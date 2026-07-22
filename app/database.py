"""Database engine and session factory with production-safe connection pooling.

Connection hardening:
- ``pool_pre_ping``: verifies connections are alive before handing them out
  (prevents stale-connection errors after Render free-tier sleep or network blips).
- ``pool_size`` / ``max_overflow``: tuned for a single-worker uvicorn process.
- ``PoolTimeout`` is deliberately *not* set here — the caller (API route or scheduler)
  should handle the timeout and return a 503 / retry instead of crashing the process.

N+1 detection (development only):
- Set ``SQLALCHEMY_ECHO=true`` env var to log all queries to ``logs/sql_queries.log``.
  Each query line includes a comment marker like ``/* N+1 guard: N=5 identical */``
  when the same query (same text, same params shape) repeats within a single request.
"""

from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

_engine_kwargs = {
    "echo": False,
    "pool_pre_ping": True,
}
if not str(settings.database_url).startswith("sqlite"):
    _engine_kwargs.update({
        "pool_size": 10,
        "max_overflow": 20,
        "pool_recycle": 3600,
        "pool_timeout": 30,
    })

engine = create_async_engine(settings.database_url, **_engine_kwargs)

# ─── N+1 / slow-query detection ──────────────────────────────────────────
# When SQLALCHEMY_ECHO=true, log all queries to a dedicated file with
# per-request query-count tracking to spot N+1 patterns.
if settings.environment in ("development", "staging") and settings.debug:
    import os as _os
    import structlog as _structlog

    _sql_logger = _structlog.get_logger("sql.queries")
    _sql_log_path = Path(__file__).resolve().parent.parent / "logs" / "sql_queries.log"
    _sql_log_path.parent.mkdir(parents=True, exist_ok=True)

    _query_counter: dict[str, int] = {}
    _slow_query_threshold_ms = int(_os.environ.get("SQL_SLOW_THRESHOLD_MS", "200"))

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _before_execute(conn, cursor, statement, parameters, context, executemany):
        import time as _time
        conn._query_start_time = _time.time()
        # Track query repetition for N+1 detection
        key = statement[:120]  # first 120 chars as fingerprint
        _query_counter[key] = _query_counter.get(key, 0) + 1

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def _after_execute(conn, cursor, statement, parameters, context, executemany):
        import time as _time
        duration = (_time.time() - conn._query_start_time) * 1000
        key = statement[:120]
        count = _query_counter.get(key, 0)
        n1_marker = f"/* N+1 guard: N={count} identical */ " if count > 1 else ""

        _sql_logger.info("sql_query",
            query=statement.strip()[:200],
            duration_ms=round(duration, 1),
            repeat_count=count,
            slow=duration > _slow_query_threshold_ms,
        )

        if duration > _slow_query_threshold_ms:
            _sql_logger.warning("slow_query",
                query=statement.strip()[:200],
                duration_ms=round(duration, 1),
                threshold_ms=_slow_query_threshold_ms,
            )

    async_session_maker = async_sessionmaker(engine, expire_on_commit=False)
else:
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