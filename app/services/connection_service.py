"""Connection Service — handles Telegram connection lifecycle."""

import asyncio
import logging
from datetime import datetime, timezone
from app.services.state_machine import SessionState, RecoveryReason, transition
from app.services.backoff import BackoffStrategy
from app.services.lock_manager import LockManager
from app.services.event_bus import EventBus

logger = logging.getLogger(__name__)


class ConnectionService:
    """Manages Telegram connections per account."""

    def __init__(self, event_bus: EventBus, lock_manager: LockManager, backoff: BackoffStrategy, health_service=None):
        self._event_bus = event_bus
        self._lock_manager = lock_manager
        self._backoff = backoff
        self._health_service = health_service
        self._states: dict[str, SessionState] = {}
        self._inflight_operations: dict[str, asyncio.Task[SessionState]] = {}
        self._inflight_lock = asyncio.Lock()
        self._event_seq = 0

    def get_state(self, account_id: str) -> SessionState:
        return self._states.get(account_id, SessionState.NOT_CONFIGURED)

    async def connect(self, account_id: str, session_string: str = "") -> SessionState:
        """Connect an account. Returns final state."""
        async with self._inflight_lock:
            existing = self._inflight_operations.get(account_id)
            if existing is not None:
                task = existing
            else:
                task = asyncio.create_task(self._connect_impl(account_id, session_string))
                self._inflight_operations[account_id] = task
        try:
            return await task
        finally:
            async with self._inflight_lock:
                if self._inflight_operations.get(account_id) is task:
                    self._inflight_operations.pop(account_id, None)

    async def _connect_impl(self, account_id: str, session_string: str = "") -> SessionState:
        async with self._lock_manager.get_lock(account_id):
            current = self.get_state(account_id)
            if current in {SessionState.CONNECTING, SessionState.CONNECTED, SessionState.RECONNECTING}:
                return current
            if current in {SessionState.EXPIRED, SessionState.UNAUTHORIZED}:
                transition_event = "re_auth"
                transition_reason = RecoveryReason.RE_AUTH
            else:
                transition_event = "register"
                transition_reason = RecoveryReason.REGISTER
            result = transition(current, transition_event, transition_reason)
            self._states[account_id] = result.current
            await self._emit(account_id, result.current, transition_reason)

        try:
            from app.services.telethon_pool import pool
            client = await pool.get_client(account_id, session_string, require_authorized=True)
            async with self._lock_manager.get_lock(account_id):
                result2 = transition(self.get_state(account_id), "connected")
                self._states[account_id] = result2.current
                self._backoff.reset(account_id)
                await self._emit(account_id, result2.current)
                return result2.current
        except Exception as exc:
            async with self._lock_manager.get_lock(account_id):
                error_event = self._classify_error(exc)
                current = self.get_state(account_id)
                result3 = transition(current, error_event)
                self._states[account_id] = result3.current
                await self._emit(account_id, result3.current, result3.reason, str(exc))
                if result3.current in {SessionState.EXPIRED, SessionState.UNAUTHORIZED}:
                    self._states[account_id] = SessionState.DISCONNECTED
                    await self._emit(account_id, SessionState.DISCONNECTED, RecoveryReason.RE_AUTH, str(exc))
                return self._states[account_id]

    async def reconnect(self, account_id: str, session_string: str = "") -> SessionState:
        """Reconnect with backoff."""
        async with self._inflight_lock:
            existing = self._inflight_operations.get(account_id)
            if existing is not None:
                task = existing
            else:
                task = asyncio.create_task(self._reconnect_impl(account_id, session_string))
                self._inflight_operations[account_id] = task
        try:
            return await task
        finally:
            async with self._inflight_lock:
                if self._inflight_operations.get(account_id) is task:
                    self._inflight_operations.pop(account_id, None)

    async def _reconnect_impl(self, account_id: str, session_string: str = "") -> SessionState:
        async with self._lock_manager.get_lock(account_id):
            current = self.get_state(account_id)
            if current in {SessionState.RECONNECTING, SessionState.CONNECTING}:
                return current
            result = transition(current, "reconnect", RecoveryReason.NETWORK)
            self._states[account_id] = result.current
            await self._emit(account_id, result.current, RecoveryReason.NETWORK)
            if current in {SessionState.CONNECTED, SessionState.DISCONNECTED}:
                self._states[account_id] = SessionState.RECONNECTING

        try:
            delay = self._backoff.next_delay(account_id)
            await asyncio.sleep(delay)

            from app.services.telethon_pool import pool
            await pool.disconnect(account_id)
            client = await pool.get_client(account_id, session_string, require_authorized=True)
            async with self._lock_manager.get_lock(account_id):
                result2 = transition(self.get_state(account_id), "connected")
                self._states[account_id] = result2.current
                self._backoff.reset(account_id)
                await self._emit(account_id, result2.current)
                return result2.current
        except Exception as exc:
            async with self._lock_manager.get_lock(account_id):
                error_event = self._classify_error(exc)
                current = self.get_state(account_id)
                result3 = transition(current, error_event)
                self._states[account_id] = result3.current
                await self._emit(account_id, result3.current, result3.reason, str(exc))
                if result3.current in {SessionState.EXPIRED, SessionState.UNAUTHORIZED}:
                    self._states[account_id] = SessionState.DISCONNECTED
                    await self._emit(account_id, SessionState.DISCONNECTED, RecoveryReason.RE_AUTH, str(exc))
                return self._states[account_id]

    async def disconnect(self, account_id: str):
        """Disconnect an account."""
        async with self._lock_manager.get_lock(account_id):
            from app.services.telethon_pool import pool
            await pool.disconnect(account_id)
            current = self.get_state(account_id)
            result = transition(current, "disconnect")
            self._states[account_id] = result.current
            await self._emit(account_id, result.current)

    async def restore_all(self, accounts: list, semaphore_limit: int = 5):
        """Restore all accounts with concurrency limit."""
        import asyncio
        semaphore = asyncio.Semaphore(semaphore_limit)

        async def _restore_one(account):
            async with semaphore:
                session_string = ""
                if hasattr(account, 'session_data') and account.session_data:
                    from app.core.crypto import decrypt_session
                    try:
                        session_string = decrypt_session(account.session_data)
                    except ValueError:
                        logger.warning(
                            "session_decrypt_failed_on_restore",
                            account_id=account.id,
                        )
                        return
                await self.connect(account.id, session_string)

        await asyncio.gather(*[_restore_one(a) for a in accounts], return_exceptions=True)

    def _classify_error(self, exc: Exception) -> str:
        exc_name = type(exc).__name__
        if "SessionInvalid" in exc_name or "Unauthorized" in exc_name:
            return "expired"
        if "Banned" in exc_name or "Deactivated" in exc_name:
            return "banned"
        if "FloodWait" in exc_name:
            return "flood_wait"
        return "failed"

    async def _emit(self, account_id: str, state: SessionState, reason: RecoveryReason | None = None, error: str | None = None):
        event_name = f"session.{state.value}"
        self._event_seq += 1
        health = None
        if self._health_service is not None:
            health = self._health_service.derive_state_health(state)
        data = {
            "version": 1,
            "event": event_name,
            "account_id": account_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "seq": self._event_seq,
            "state": state.value,
            "reason": reason.value if reason else None,
            "error": error,
            "health": health,
        }
        await self._event_bus.publish(event_name, data)
