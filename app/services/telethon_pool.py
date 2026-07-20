import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import settings
from app.core.logging import get_logger


@dataclass
class PendingAuth:
    phone_code_hash: str


class SessionInvalidError(Exception):
    """Raised when a pooled TelegramClient's session is no longer authorized."""


logger = get_logger(__name__)


class TelethonClientPool:
    """Keeps one TelegramClient alive per account across the multi-step login flow
    (send-code -> verify-code -> verify-2fa) and for later status checks.

    In-memory only: state is lost on process restart and is not shared across worker
    processes. Fine for a single personal-use uvicorn process; a multi-worker deployment
    would need a shared store (e.g. Redis) instead.
    """

    MAX_RECONNECT_ATTEMPTS = 3
    RECONNECT_DELAY_SECONDS = 2
    DISCONNECT_TIMEOUT_SECONDS = 5

    def __init__(self) -> None:
        self._clients: dict[str, TelegramClient] = {}
        self._pending_auth: dict[str, PendingAuth] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, account_id: str) -> asyncio.Lock:
        lock = self._locks.get(account_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[account_id] = lock
        return lock

    async def get_client(
        self, account_id: str, session_string: str = "", *, require_authorized: bool = True
    ) -> TelegramClient:
        async with self._lock_for(account_id):
            client = self._clients.get(account_id)
            if client is None:
                api_id, api_hash = settings.telegram_credentials
                client = TelegramClient(StringSession(session_string), api_id, api_hash)
                self._clients[account_id] = client
            connect_check_start = datetime.now(timezone.utc)
            is_connected = client.is_connected()
            connect_check_elapsed = (datetime.now(timezone.utc) - connect_check_start).total_seconds()
            logger.info(
                "telethon_pool_connect_check",
                account_id=account_id,
                is_connected=is_connected,
                elapsed_seconds=round(connect_check_elapsed, 4),
            )
            if not is_connected:
                for attempt in range(1, self.MAX_RECONNECT_ATTEMPTS + 1):
                    attempt_start = datetime.now(timezone.utc)
                    try:
                        await client.connect()
                        attempt_elapsed = (datetime.now(timezone.utc) - attempt_start).total_seconds()
                        logger.info(
                            "telethon_reconnect_succeeded",
                            account_id=account_id,
                            attempt=attempt,
                            elapsed_seconds=round(attempt_elapsed, 4),
                        )
                        break
                    except Exception as exc:
                        attempt_elapsed = (datetime.now(timezone.utc) - attempt_start).total_seconds()
                        logger.warning(
                            "telethon_reconnect_attempt",
                            account_id=account_id,
                            attempt=attempt,
                            max_attempts=self.MAX_RECONNECT_ATTEMPTS,
                            error=str(exc),
                            elapsed_seconds=round(attempt_elapsed, 4),
                        )
                        if attempt < self.MAX_RECONNECT_ATTEMPTS:
                            await asyncio.sleep(self.RECONNECT_DELAY_SECONDS)
                        else:
                            logger.error(
                                "telethon_reconnect_exhausted",
                                account_id=account_id,
                                error=str(exc),
                            )
                            raise
            if require_authorized and session_string and not await client.is_user_authorized():
                self._clients.pop(account_id, None)
                self._pending_auth.pop(account_id, None)
                logger.warning("telethon_session_invalid", account_id=account_id)
                raise SessionInvalidError(
                    f"Telegram session is no longer authorized for account {account_id}"
                )
            return client

    def peek_client(self, account_id: str) -> TelegramClient | None:
        """Returns the pooled client if one already exists, without creating or
        connecting one — used by callers (e.g. auto-reply toggle-off) that only need to
        act on an already-live client and should no-op if there isn't one."""
        return self._clients.get(account_id)

    def set_pending_auth(self, account_id: str, phone_code_hash: str) -> None:
        self._pending_auth[account_id] = PendingAuth(phone_code_hash=phone_code_hash)

    def get_pending_auth(self, account_id: str) -> PendingAuth | None:
        return self._pending_auth.get(account_id)

    def clear_pending_auth(self, account_id: str) -> None:
        self._pending_auth.pop(account_id, None)

    async def remove_client(self, account_id: str) -> None:
        async with self._lock_for(account_id):
            client = self._clients.pop(account_id, None)
        self._pending_auth.pop(account_id, None)
        if client is not None:
            try:
                await asyncio.wait_for(
                    client.disconnect(),
                    timeout=self.DISCONNECT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("telethon_disconnect_timeout", account_id=account_id)
            except Exception as exc:
                logger.warning("telethon_disconnect_error", account_id=account_id, error=str(exc))

    async def disconnect_all(self) -> None:
        tasks = []
        for account_id in list(self._clients):
            tasks.append(self.remove_client(account_id))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("telethon_pool_disconnected", count=len(tasks))


pool = TelethonClientPool()
