import asyncio
from dataclasses import dataclass

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import settings


@dataclass
class PendingAuth:
    phone_code_hash: str


class TelethonClientPool:
    """Keeps one TelegramClient alive per account across the multi-step login flow
    (send-code -> verify-code -> verify-2fa) and for later status checks.

    In-memory only: state is lost on process restart and is not shared across worker
    processes. Fine for a single personal-use uvicorn process; a multi-worker deployment
    would need a shared store (e.g. Redis) instead.
    """

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

    async def get_client(self, account_id: str, session_string: str = "") -> TelegramClient:
        async with self._lock_for(account_id):
            client = self._clients.get(account_id)
            if client is None:
                api_id, api_hash = settings.telegram_credentials
                client = TelegramClient(StringSession(session_string), api_id, api_hash)
                self._clients[account_id] = client
            if not client.is_connected():
                await client.connect()
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
            await client.disconnect()

    async def disconnect_all(self) -> None:
        for account_id in list(self._clients):
            await self.remove_client(account_id)


pool = TelethonClientPool()
