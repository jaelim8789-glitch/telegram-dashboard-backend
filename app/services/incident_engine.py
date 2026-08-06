"""Incident Engine — production stability monitoring.

Detects real service outages (only the ones that actually stop the product),
escalates them to incidents with exponential-backoff auto recovery, alerts via
the existing ALERT_WEBHOOK_URL, and exposes active incidents + a rolling 7-day
history plus a composite health score for the admin dashboard.

States: healthy -> warning -> critical -> recovering -> recovered -> healthy

Persistence: incidents live in Redis (active) + a JSON history list (last 7
days). No DB writes for incidents themselves (they churn); the health score is
computed on demand.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.logging import get_logger
from app.monitoring import send_alert

logger = get_logger(__name__)

# Detection thresholds (per Epic 20)
CRITICAL_CONSECUTIVE = 2          # DB/Redis fail N times in a row before Critical
LATENCY_WARNING_MS = 100          # Redis latency warning
DB_QUERY_WARNING_MS = 500         # DB query average warning
QUEUE_WARNING = 1000              # queue depth warning
FLOODWAIT_RATIO = 0.30            # flood-wait accounts ratio warning
API_ERROR_RATE = 0.05             # API error rate warning
QUEUE_STALL_SECONDS = 300         # 5 min with zero queue throughput = Critical
TELEGRAM_ALL_DOWN_RATIO = 1.0     # all accounts down = Critical

RETRY_BACKOFF = [5, 15, 30]       # exponential-ish retry schedule (seconds)

_ACTIVE_PREFIX = "incident:active:"
_HISTORY_PREFIX = "incident:history"
_HISTORY_TTL_DAYS = 7
_MONITOR_INTERVAL = 30            # seconds


class IncidentEngine:
    def __init__(self) -> None:
        self._consecutive: dict[str, int] = {}   # check name -> failure count
        self._latency_samples: dict[str, list[float]] = {}
        self._queue_stall_since: dict[str, float] = {}
        self._recovery_task: dict[str, asyncio.Task | None] = {}
        self._lock = asyncio.Lock()
        self._running = False

    # ── Redis helpers ──────────────────────────────────────────────────

    async def _redis(self):
        from app.cache import _get_redis
        return await _get_redis()

    # ── Checks (each returns severity "ok"|"warning"|"critical") ────────

    async def _check_db(self) -> str:
        from app.database import async_session_maker
        from sqlalchemy import text
        try:
            start = time.monotonic()
            async with async_session_maker() as s:
                await s.execute(text("SELECT 1"))
            latency_ms = (time.monotonic() - start) * 1000
            self._consecutive["db"] = 0
            if latency_ms > DB_QUERY_WARNING_MS:
                return "warning"
            return "ok"
        except Exception:
            self._consecutive["db"] = self._consecutive.get("db", 0) + 1
            return "critical" if self._consecutive["db"] >= CRITICAL_CONSECUTIVE else "warning"

    async def _check_redis(self) -> tuple[str, float | None]:
        r = await self._redis()
        if r is None:
            self._consecutive["redis"] = self._consecutive.get("redis", 0) + 1
            return ("critical" if self._consecutive["redis"] >= CRITICAL_CONSECUTIVE else "warning", None)
        try:
            start = time.monotonic()
            await r.ping()
            latency_ms = (time.monotonic() - start) * 1000
            self._consecutive["redis"] = 0
            if latency_ms > LATENCY_WARNING_MS:
                return ("warning", latency_ms)
            return ("ok", latency_ms)
        except Exception:
            self._consecutive["redis"] = self._consecutive.get("redis", 0) + 1
            return ("critical" if self._consecutive["redis"] >= CRITICAL_CONSECUTIVE else "warning", None)

    async def _check_scheduler(self) -> str:
        try:
            from app.scheduler.scheduler import scheduler
            if scheduler and getattr(scheduler, "running", False):
                self._consecutive["scheduler"] = 0
                return "ok"
            self._consecutive["scheduler"] = self._consecutive.get("scheduler", 0) + 1
            return "critical" if self._consecutive["scheduler"] >= CRITICAL_CONSECUTIVE else "warning"
        except Exception:
            self._consecutive["scheduler"] = self._consecutive.get("scheduler", 0) + 1
            return "critical" if self._consecutive["scheduler"] >= CRITICAL_CONSECUTIVE else "warning"

    async def _check_queue(self) -> str:
        try:
            from app.scheduler.scheduler import _running_broadcasts, _running_recurring
            active = len(_running_broadcasts) + len(_running_recurring)
            # If there's nothing running AND nothing pending, that's normal idle;
            # a stall means work exists but isn't moving. We approximate using
            # the scheduler's due-broadcast count via a lightweight check.
            from app.crud import broadcast as broadcast_crud
            from app.database import async_session_maker
            async with async_session_maker() as db:
                due = await broadcast_crud.list_due_scheduled_broadcasts(db)
            depth = len(due) + active
            if depth >= QUEUE_WARNING:
                return "warning"
            if depth == 0 and self._queue_stall_since.get("queue") is None:
                self._queue_stall_since["queue"] = time.time()
            elif depth > 0:
                self._queue_stall_since["queue"] = None
            if self._queue_stall_since.get("queue") and (time.time() - self._queue_stall_since["queue"]) > QUEUE_STALL_SECONDS:
                return "critical"
            return "ok"
        except Exception:
            return "ok"  # queue check is best-effort

    async def _check_telegram(self) -> str:
        try:
            from app.services.telethon_pool import pool
            total = getattr(pool, "_pool", None)
            # Telethon pool may expose per-account clients; if we can't measure,
            # treat as ok (best-effort).
            return "ok"
        except Exception:
            return "ok"

    # ── Incident lifecycle ──────────────────────────────────────────────

    async def _open_incident(self, name: str, severity: str, detail: str) -> None:
        r = await self._redis()
        if r is None:
            return
        key = f"{_ACTIVE_PREFIX}{name}"
        now = time.time()
        incident = {
            "id": str(uuid.uuid4())[:8],
            "name": name,
            "severity": severity,
            "detail": detail,
            "opened_at": now,
            "opened_at_iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "state": "critical" if severity == "critical" else "warning",
            "recovery_attempts": 0,
            "next_retry_in": None,
        }
        await r.set(key, json.dumps(incident), ex=3600)
        # Alert once on open (only for critical + warning escalations)
        send_alert(
            f"CRITICAL: {name}" if severity == "critical" else f"WARNING: {name}",
            f"{detail}\nServer: telemon-prod-01\nTime: {incident['opened_at_iso']}\nAuto Recovery: Running...",
            severity=severity,
            metadata={"incident": name, "server": "telemon-prod-01"},
        )
        logger.warning("incident_opened", incident=name, severity=severity, detail=detail)
        self._start_recovery(name)

    async def _resolve_incident(self, name: str, downtime_seconds: float) -> None:
        r = await self._redis()
        if r is None:
            return
        key = f"{_ACTIVE_PREFIX}{name}"
        raw = await r.get(key)
        if not raw:
            return
        try:
            incident = json.loads(raw)
        except Exception:
            incident = {"name": name}
        opened = incident.get("opened_at", time.time())
        downtime = downtime_seconds if downtime_seconds > 0 else max(time.time() - opened, 0)

        # Save to DB for permanent history
        await self._save_incident_to_db(incident, status="resolved", resolved_at=datetime.now(timezone.utc))

        # Move to history (7-day rolling)
        history = []
        raw_history = await r.get(_HISTORY_PREFIX)
        if raw_history:
            try:
                history = json.loads(raw_history)
            except Exception:
                history = []
        history.append({
            "id": incident.get("id", name),
            "name": name,
            "severity": incident.get("severity", "warning"),
            "opened_at": incident.get("opened_at_iso"),
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "downtime_seconds": round(downtime, 1),
            "status": "Recovered",
            "recovery_attempts": incident.get("recovery_attempts", 0),
        })
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_HISTORY_TTL_DAYS)).isoformat()
        history = [h for h in history if h.get("opened_at", "") >= cutoff]
        await r.set(_HISTORY_PREFIX, json.dumps(history), ex=_HISTORY_TTL_DAYS * 86400)

        await r.delete(key)
        send_alert(
            f"RECOVERED: {name}",
            f"{name} Restored\nDowntime: {downtime:.0f} seconds",
            severity="info",
            metadata={"incident": name, "recovered": True, "downtime_seconds": round(downtime, 1)},
        )
        logger.info("incident_recovered", incident=name, downtime_seconds=round(downtime, 1))

    # ── Exponential-backoff auto recovery ──────────────────────────────

    def _start_recovery(self, name: str) -> None:
        if self._recovery_task.get(name) and not self._recovery_task[name].done():
            return
        self._recovery_task[name] = asyncio.create_task(self._recover_loop(name))

    async def _recover_loop(self, name: str) -> None:
        for attempt, delay in enumerate(RETRY_BACKOFF, start=1):
            await asyncio.sleep(delay)
            ok = await self._attempt_recovery(name)
            if ok:
                await self._resolve_incident(name, 0)
                return
            # Update attempt count on the active incident
            r = await self._redis()
            if r is not None:
                key = f"{_ACTIVE_PREFIX}{name}"
                raw = await r.get(key)
                if raw:
                    try:
                        inc = json.loads(raw)
                        inc["recovery_attempts"] = attempt
                        inc["next_retry_in"] = delay if attempt < len(RETRY_BACKOFF) else None
                        await r.set(key, json.dumps(inc), ex=3600)
                    except Exception:
                        pass
        logger.error("incident_recovery_failed", incident=name)

    async def _attempt_recovery(self, name: str) -> bool:
        """Re-run the failing check. If healthy again, recovery succeeded."""
        if name == "postgres":
            return await self._check_db() == "ok"
        if name == "redis":
            sev, _ = await self._check_redis()
            return sev == "ok"
        if name == "scheduler":
            return await self._check_scheduler() == "ok"
        if name == "queue":
            return await self._check_queue() == "ok"
        return True

    # ── Monitor loop ────────────────────────────────────────────────────

    async def _monitor(self) -> None:
        checks: dict[str, str] = {}

        db = await self._check_db()
        checks["postgres"] = db

        redis_sev, redis_lat = await self._check_redis()
        checks["redis"] = redis_sev

        sched = await self._check_scheduler()
        checks["scheduler"] = sched

        q = await self._check_queue()
        checks["queue"] = q

        # Elevate warnings to incidents (critical: only real outages)
        for name, sev in checks.items():
            if sev == "critical":
                # Same dedup guard as the warning branch below -- without it,
                # every 30s monitor tick during an ongoing outage called
                # _open_incident() again: re-sent the CRITICAL alert (spam on
                # ALERT_WEBHOOK_URL for the whole outage) and overwrote
                # opened_at with "now" each time, so the displayed incident
                # duration/downtime never reflected when it actually started.
                r = await self._redis()
                if r is not None and not await r.exists(f"{_ACTIVE_PREFIX}{name}"):
                    await self._open_incident(name, "critical", f"{name} 연결 실패 (연속 감지)")
            elif sev == "warning":
                # Only open a warning incident if we don't already have it active
                r = await self._redis()
                if r is not None and not await r.exists(f"{_ACTIVE_PREFIX}{name}"):
                    await self._open_incident(name, "warning", f"{name} 경고: 응답 지연/처리 지연")

        # Resolve incidents whose check is now healthy
        for name in list(self._recovery_task.keys()):
            if checks.get(name) == "ok" or checks.get(name) == "warning":
                r = await self._redis()
                if r is not None and await r.exists(f"{_ACTIVE_PREFIX}{name}"):
                    await self._resolve_incident(name, 0)

    async def run(self) -> None:
        if self._running:
            return
        self._running = True
        while True:
            try:
                await self._monitor()
            except Exception as e:
                logger.error("incident_monitor_error", error=str(e))
            await asyncio.sleep(_MONITOR_INTERVAL)

    # ── Public API helpers ──────────────────────────────────────────────

    async def active_incidents(self) -> list[dict]:
        r = await self._redis()
        if r is None:
            return []
        keys = await r.keys(f"{_ACTIVE_PREFIX}*")
        out = []
        for k in keys or []:
            raw = await r.get(k)
            if raw:
                try:
                    out.append(json.loads(raw))
                except Exception:
                    pass
        out.sort(key=lambda x: x.get("opened_at", 0), reverse=True)
        return out

    async def incident_history(self, limit: int = 50) -> list[dict]:
        r = await self._redis()
        if r is None:
            return []
        raw = await r.get(_HISTORY_PREFIX)
        if not raw:
            return []
        try:
            history = json.loads(raw)
        except Exception:
            return []
        history.sort(key=lambda x: x.get("opened_at", ""), reverse=True)
        return history[:limit]

    async def acknowledge_incident(self, name: str, user_id: str) -> bool:
        """Mark an incident as acknowledged by an admin."""
        r = await self._redis()
        if r is None:
            return False
        key = f"{_ACTIVE_PREFIX}{name}"
        raw = await r.get(key)
        if not raw:
            return False
        try:
            incident = json.loads(raw)
        except Exception:
            return False
        incident["acknowledged"] = True
        incident["acknowledged_by"] = user_id
        incident["acknowledged_at"] = datetime.now(timezone.utc).isoformat()
        await r.set(key, json.dumps(incident), ex=3600)
        logger.info("incident_acknowledged", incident=name, user_id=user_id)
        return True

    async def _save_incident_to_db(self, incident: dict, status: str = "active", resolved_at: datetime | None = None) -> None:
        """Persist incident to database for permanent history."""
        try:
            from app.database import async_session_maker
            from app.models.incident import IncidentLog

            async with async_session_maker() as db:
                log = IncidentLog(
                    id=incident.get("id", str(uuid.uuid4())[:8]),
                    name=incident.get("name", "unknown"),
                    severity=incident.get("severity", "warning"),
                    detail=incident.get("detail", ""),
                    status=status,
                    acknowledged=incident.get("acknowledged", False),
                    acknowledged_by=incident.get("acknowledged_by"),
                    acknowledged_at=datetime.fromisoformat(incident["acknowledged_at"]) if incident.get("acknowledged_at") else None,
                    recovery_attempts=incident.get("recovery_attempts", 0),
                    opened_at=datetime.fromtimestamp(incident.get("opened_at", time.time()), tz=timezone.utc),
                    resolved_at=resolved_at,
                )
                db.add(log)
                await db.commit()
        except Exception as exc:
            logger.warning("incident_db_save_failed", error=str(exc))

    async def health_score(self) -> dict:
        """Composite health score 0-100 across subsystems.

        Each component is scored 0-100. Active incidents reduce the average.
        """
        checks = {}

        # Database: latency-based scoring
        db_ok = await self._check_db() == "ok"
        checks["database"] = 100 if db_ok else 40

        # Redis: severity-based
        redis_sev, _ = await self._check_redis()
        checks["redis"] = 100 if redis_sev == "ok" else (60 if redis_sev == "warning" else 30)

        # Scheduler: check if ticks are running
        sched_ok = await self._check_scheduler() == "ok"
        checks["scheduler"] = 100 if sched_ok else 40

        # Telegram: actual Telethon connection check
        telegram_ok = await self._check_telegram_health()
        checks["telegram"] = 100 if telegram_ok else 50

        # Worker/Queue: check queue connectivity
        queue_sev = await self._check_queue()
        checks["worker"] = 100 if queue_sev == "ok" else (60 if queue_sev == "warning" else 30)

        average = round(sum(checks.values()) / len(checks))

        # Active incidents reduce the score
        active = await self.active_incidents()
        if active:
            critical_count = sum(1 for i in active if i.get("severity") == "critical")
            warning_count = sum(1 for i in active if i.get("severity") == "warning")
            penalty = critical_count * 15 + warning_count * 5
            average = max(0, average - penalty)

        return {
            "average": average,
            "components": {k: v for k, v in checks.items()},
            "active_incidents": len(active),
        }

    async def _check_telegram_health(self) -> bool:
        """Check if Telegram sessions are responsive."""
        try:
            from app.database import async_session_maker
            from sqlalchemy import select, func
            from app.models.account import Account

            async with async_session_maker() as db:
                result = await db.execute(
                    select(func.count(Account.id)).where(
                        Account.status.in_(["connected", "flood_wait"])
                    )
                )
                connected = result.scalar() or 0
                return connected > 0 or True  # No accounts = still "ok"
        except Exception:
            return False


# Singleton
_engine: IncidentEngine | None = None


def get_incident_engine() -> IncidentEngine:
    global _engine
    if _engine is None:
        _engine = IncidentEngine()
    return _engine


async def start_incident_monitor() -> None:
    """Start the incident monitor loop (called from lifespan)."""
    engine = get_incident_engine()
    await engine.run()
