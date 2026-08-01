"""SessionManager — Coordinator.

Thin orchestrator. Actual logic lives in:
  ConnectionService, HealthService, EventBus, LockManager, BackoffStrategy,
  MetricsCollector, EventLogger
"""

import logging
import asyncio
from typing import ClassVar, Optional
from app.services.event_bus import EventBus, MemoryEventBus
from app.services.state_machine import SessionState, RecoveryReason
from app.services.connection_service import ConnectionService
from app.services.health_service import HealthService
from app.services.backoff import BackoffStrategy
from app.services.lock_manager import LockManager
from app.services.metrics_collector import MetricsCollector
from app.services.event_logger import EventLogger

logger = logging.getLogger(__name__)


class SessionManager:
    """Singleton coordinator for all Telegram session lifecycle."""

    _instance: ClassVar[Optional["SessionManager"]] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = False
        self._event_bus: EventBus = MemoryEventBus()
        self._lock_manager = LockManager()
        self._backoff = BackoffStrategy()
        self._connection_service = ConnectionService(self._event_bus, self._lock_manager, self._backoff)
        self._health_service = HealthService()
        self._metrics = MetricsCollector()
        self._event_logger: EventLogger | None = None
        self._monitor_task: asyncio.Task | None = None
        self._db_session_factory = None

    async def initialize(self, db_session_factory):
        """App startup — called once."""
        if self._initialized:
            return
        self._db_session_factory = db_session_factory
        self._event_logger = EventLogger(db_session_factory)
        self._event_bus.subscribe("session.*", self._on_any_session_event)
        self._initialized = True
        logger.info("session_manager_initialized")

    async def start(self, accounts: list):
        """Restore all sessions on startup."""
        await self._connection_service.restore_all(accounts)
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        """Graceful shutdown."""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        await self._event_bus.drain()
        from app.services.telethon_pool import pool
        await pool.disconnect_all()
        logger.info("session_manager_stopped")

    async def connect(self, account_id: str, session_string: str = "") -> SessionState:
        return await self._connection_service.connect(account_id, session_string)

    async def reconnect(self, account_id: str, session_string: str = "") -> SessionState:
        return await self._connection_service.reconnect(account_id, session_string)

    async def disconnect(self, account_id: str):
        await self._connection_service.disconnect(account_id)

    def get_state(self, account_id: str) -> SessionState:
        return self._connection_service.get_state(account_id)

    def get_metrics(self) -> dict:
        return self._metrics.to_dict()

    async def get_events(self, account_id: str, limit: int = 50) -> list:
        if self._event_logger:
            return await self._event_logger.get_events(account_id, limit)
        return []

    async def _monitor_loop(self):
        """Periodic health check every 10 seconds."""
        while True:
            try:
                await asyncio.sleep(10)
                for account_id, state in list(self._connection_service._states.items()):
                    if state == SessionState.CONNECTED:
                        try:
                            from app.services.telethon_pool import pool
                            client = await pool.get_client(account_id, require_authorized=True)
                        except Exception:
                            await self._connection_service.reconnect(account_id)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("monitor_error", error=str(exc))

    async def _on_any_session_event(self, data: dict):
        """Subscribe to all session.* events — update metrics + log."""
        self._metrics.record_event()
        if self._event_logger:
            await self._event_logger.log(
                account_id=data.get("account_id", ""),
                event=data.get("event", data.get("state", "")),
                health=data.get("state", ""),
                reason=data.get("reason"),
                error=data.get("error"),
            )
