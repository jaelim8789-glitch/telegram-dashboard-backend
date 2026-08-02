"""Event Logger — persists session events to DB, keeps last 100 per account.

The session_event_logs table is created automatically on first use if it
does not already exist, so no Alembic migration is required.
"""

import logging
from datetime import datetime, timezone
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class EventLogger:
    def __init__(self, session_factory):
        self._session_factory = session_factory
        self._ensured = False

    async def _ensure_table(self):
        """Create the session_event_logs table if it does not exist."""
        if self._ensured:
            return
        async with self._session_factory() as db:
            await db.execute(text("""
                CREATE TABLE IF NOT EXISTS session_event_logs (
                    id SERIAL PRIMARY KEY,
                    account_id VARCHAR(36) NOT NULL,
                    event VARCHAR(50) NOT NULL,
                    health VARCHAR(30) NOT NULL,
                    reason VARCHAR(30),
                    error TEXT,
                    meta JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            await db.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_session_event_logs_account_id
                ON session_event_logs (account_id)
            """))
            await db.commit()
        self._ensured = True
        logger.info("session_event_logs_table_ensured")

    async def log(self, account_id: str, event: str, health: str, reason: str | None = None, error: str | None = None, meta: dict | None = None):
        await self._ensure_table()
        from app.models.session_event_log import SessionEventLog
        async with self._session_factory() as db:
            db.add(SessionEventLog(
                account_id=account_id,
                event=event,
                health=health,
                reason=reason,
                error=error,
                meta=meta,
            ))
            count = await db.scalar(
                select(func.count()).where(SessionEventLog.account_id == account_id)
            )
            if count and count > 100:
                oldest = await db.scalars(
                    select(SessionEventLog)
                    .where(SessionEventLog.account_id == account_id)
                    .order_by(SessionEventLog.created_at.asc())
                    .limit(count - 100)
                )
                for row in oldest:
                    await db.delete(row)
            await db.commit()

    async def get_events(self, account_id: str, limit: int = 50) -> list:
        await self._ensure_table()
        from app.models.session_event_log import SessionEventLog
        async with self._session_factory() as db:
            result = await db.scalars(
                select(SessionEventLog)
                .where(SessionEventLog.account_id == account_id)
                .order_by(SessionEventLog.created_at.desc())
                .limit(limit)
            )
            return [
                {
                    "id": row.id,
                    "account_id": row.account_id,
                    "event": row.event,
                    "health": row.health,
                    "reason": row.reason,
                    "error": row.error,
                    "meta": row.meta,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in result.all()
            ]
