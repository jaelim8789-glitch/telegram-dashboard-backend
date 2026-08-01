"""Connection Service — handles Telegram connection lifecycle."""

import logging
from app.services.state_machine import SessionState, RecoveryReason, transition
from app.services.backoff import BackoffStrategy
from app.services.lock_manager import LockManager
from app.services.event_bus import EventBus

logger = logging.getLogger(__name__)


class ConnectionService:
    """Manages Telegram connections per account."""

    def __init__(self, event_bus: EventBus, lock_manager: LockManager, backoff: BackoffStrategy):
        self._event_bus = event_bus
        self._lock_manager = lock_manager
        self._backoff = backoff
        self._states: dict[str, SessionState] = {}

    def get_state(self, account_id: str) -> SessionState:
        return self._states.get(account_id, SessionState.NOT_CONFIGURED)

    async def connect(self, account_id: str, session_string: str = "") -> SessionState:
        """Connect an account. Returns final state."""
        async with self._lock_manager.get_lock(account_id):
            current = self.get_state(account_id)
            result = transition(current, "register", RecoveryReason.REGISTER)
            self._states[account_id] = result.current
            await self._emit(account_id, result.current, RecoveryReason.REGISTER)

            try:
                from app.services.telethon_pool import pool
                client = await pool.get_client(account_id, session_string, require_authorized=True)
                result2 = transition(result.current, "connected")
                self._states[account_id] = result2.current
                self._backoff.reset(account_id)
                await self._emit(account_id, result2.current)
                return result2.current
            except Exception as exc:
                error_event = self._classify_error(exc)
                result3 = transition(result.current, error_event)
                self._states[account_id] = result3.current
                await self._emit(account_id, result3.current, result3.reason, str(exc))
                return result3.current

    async def reconnect(self, account_id: str, session_string: str = "") -> SessionState:
        """Reconnect with backoff."""
        async with self._lock_manager.get_lock(account_id):
            current = self.get_state(account_id)
            result = transition(current, "reconnect", RecoveryReason.NETWORK)
            self._states[account_id] = result.current
            await self._emit(account_id, result.current, RecoveryReason.NETWORK)

            delay = self._backoff.next_delay(account_id)
            import asyncio
            await asyncio.sleep(delay)

            try:
                from app.services.telethon_pool import pool
                await pool.disconnect(account_id)
                client = await pool.get_client(account_id, session_string, require_authorized=True)
                result2 = transition(result.current, "connected")
                self._states[account_id] = result2.current
                self._backoff.reset(account_id)
                await self._emit(account_id, result2.current)
                return result2.current
            except Exception as exc:
                error_event = self._classify_error(exc)
                result3 = transition(result.current, error_event)
                self._states[account_id] = result3.current
                await self._emit(account_id, result3.current, result3.reason, str(exc))
                return result3.current

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
                    session_string = decrypt_session(account.session_data)
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
        data = {
            "account_id": account_id,
            "state": state.value,
            "reason": reason.value if reason else None,
            "error": error,
        }
        await self._event_bus.publish(event_name, data)
