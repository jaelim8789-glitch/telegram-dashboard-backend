import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import settings
from app.core.logging import get_logger

_flood_wait_until: dict[str, float] = {}
REDIS_PENDING_AUTH_TTL = 300  # 5 minutes


def is_account_flood_limited(account_id: str) -> tuple[bool, float]:
    wait = _flood_wait_until.get(account_id, 0)
    remaining = wait - time.time()
    return remaining > 0, max(0, remaining)


def record_flood_wait(account_id: str, seconds: float) -> None:
    _flood_wait_until[account_id] = time.time() + seconds


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
                client = TelegramClient(StringSession(session_string), api_id, api_hash, flood_sleep_threshold=0)
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
            else:
                try:
                    await client.get_me()
                except Exception:
                    logger.warning(
                        "telethon_zombie_detected",
                        account_id=account_id,
                    )
                    for attempt in range(1, self.MAX_RECONNECT_ATTEMPTS + 1):
                        attempt_start = datetime.now(timezone.utc)
                        try:
                            await client.connect()
                            attempt_elapsed = (datetime.now(timezone.utc) - attempt_start).total_seconds()
                            logger.info(
                                "telethon_zombie_reconnect_succeeded",
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
        connecting one  used by callers (e.g. auto-reply toggle-off) that only need to
        act on an already-live client and should no-op if there isn't one."""
        return self._clients.get(account_id)

    def set_pending_auth(self, account_id: str, phone_code_hash: str) -> None:
        """Store phone_code_hash in memory AND Redis for restart resilience."""
        self._pending_auth[account_id] = PendingAuth(phone_code_hash=phone_code_hash)
        try:
            asyncio.get_event_loop().create_task(
                self._backup_pending_auth(account_id, phone_code_hash)
            )
        except Exception:
            pass

    async def _backup_pending_auth(self, account_id: str, phone_code_hash: str) -> None:
        try:
            from app.cache import set as cache_set
            await cache_set(f"pending_auth:{account_id}", phone_code_hash, ttl=REDIS_PENDING_AUTH_TTL)
        except Exception:
            pass

    def get_pending_auth(self, account_id: str) -> PendingAuth | None:
        """Get from memory first; if missing, try Redis recovery."""
        result = self._pending_auth.get(account_id)
        if result is not None:
            return result
        # Memory miss — schedule async Redis recovery for next lookup
        if account_id not in self._pending_auth:
            try:
                asyncio.get_event_loop().create_task(self._recover_pending_auth(account_id))
            except Exception:
                pass
        return None

    async def _recover_pending_auth(self, account_id: str) -> None:
        """Recover pending_auth from Redis after server restart."""
        try:
            from app.cache import get as cache_get
            phone_code_hash = await cache_get(f"pending_auth:{account_id}")
            if phone_code_hash:
                self._pending_auth[account_id] = PendingAuth(phone_code_hash=phone_code_hash)
        except Exception:
            pass

    def clear_pending_auth(self, account_id: str) -> None:
        """Clear from both memory and Redis."""
        self._pending_auth.pop(account_id, None)
        try:
            asyncio.get_event_loop().create_task(self._clear_redis_pending_auth(account_id))
        except Exception:
            pass

    async def _clear_redis_pending_auth(self, account_id: str) -> None:
        try:
            from app.cache import delete as cache_delete
            await cache_delete(f"pending_auth:{account_id}")
        except Exception:
            pass

    async def disconnect(self, account_id: str) -> None:
        client = self._clients.pop(account_id, None)
        if client is not None and client.is_connected():
            try:
                await asyncio.wait_for(
                    client.disconnect(),
                    timeout=self.DISCONNECT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("telethon_disconnect_timeout", account_id=account_id)
            except Exception as exc:
                logger.warning("telethon_disconnect_error", account_id=account_id, error=str(exc))

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
