"""Telegram Adapter — wraps existing Telethon pool logic."""

from app.services.adapters.base import SessionAdapter
from app.services.state_machine import SessionState


class TelegramAdapter(SessionAdapter):
    def __init__(self, pool):
        self._pool = pool

    @property
    def platform(self) -> str:
        return "telegram"

    async def connect(self, account_id: str, credentials: dict) -> SessionState:
        session_string = credentials.get("session_string", "")
        try:
            client = await self._pool.get_client(account_id, session_string, require_authorized=True)
            return SessionState.CONNECTED
        except Exception:
            return SessionState.DISCONNECTED

    async def disconnect(self, account_id: str) -> None:
        await self._pool.disconnect(account_id)

    async def validate(self, account_id: str) -> SessionState:
        try:
            client = await self._pool.get_client(account_id, require_authorized=True)
            return SessionState.CONNECTED
        except Exception:
            return SessionState.DISCONNECTED

    async def get_health(self, account_id: str) -> dict:
        return {"platform": "telegram", "account_id": account_id}
