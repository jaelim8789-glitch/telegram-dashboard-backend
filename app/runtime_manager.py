"""Minimal runtime_manager adapter for telegram-dashboard-backend.

Provides just enough RuntimeManager surface for the ported draft routes
to create broadcasts through the existing app.crud layer.
"""

from __future__ import annotations

from typing import Any

from app.crud import broadcast as broadcast_crud
from app.database import async_session_maker
from app.schemas.broadcast import BroadcastCreate


class RuntimeManager:
    _instance: RuntimeManager | None = None

    @classmethod
    def get_instance(cls) -> RuntimeManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def create_broadcast(self, broadcast_input: BroadcastCreate) -> Any:
        async with async_session_maker() as session:
            broadcast = await broadcast_crud.create_broadcast(
                session,
                broadcast_input,
                media_path=None,
                scheduled_at=broadcast_input.scheduled_at,
            )
            await session.commit()
            await session.refresh(broadcast)
            return broadcast
