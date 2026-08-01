"""Event Bus for session state changes.

Architecture:
  EventBus (Interface)
    ├── MemoryEventBus (single-process, current)
    ├── RedisEventBus (multi-worker, future)
    └── KafkaEventBus (distributed, future)
"""

from abc import ABC, abstractmethod
from typing import Callable, Any
import asyncio
import logging

logger = logging.getLogger(__name__)


class EventBus(ABC):
    """Abstract event bus interface."""

    @abstractmethod
    async def publish(self, event: str, data: dict[str, Any]) -> None:
        ...

    @abstractmethod
    def subscribe(self, event: str, callback: Callable) -> None:
        ...

    @abstractmethod
    def unsubscribe(self, event: str, callback: Callable) -> None:
        ...

    @abstractmethod
    async def drain(self) -> None:
        """Wait for all pending events to be processed."""
        ...


class MemoryEventBus(EventBus):
    """In-memory event bus for single-process deployments."""

    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = {}
        self._pending: list[asyncio.Task] = []

    async def publish(self, event: str, data: dict[str, Any]) -> None:
        for callback in self._subscribers.get(event, []):
            task = asyncio.create_task(self._safe_call(callback, event, data))
            self._pending.append(task)
            task.add_done_callback(lambda t: self._pending.discard(t) if hasattr(self._pending, 'discard') else None)

    def subscribe(self, event: str, callback: Callable) -> None:
        if event not in self._subscribers:
            self._subscribers[event] = []
        self._subscribers[event].append(callback)

    def unsubscribe(self, event: str, callback: Callable) -> None:
        if event in self._subscribers:
            self._subscribers[event] = [c for c in self._subscribers[event] if c != callback]

    async def drain(self) -> None:
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        self._pending.clear()

    async def _safe_call(self, callback: Callable, event: str, data: dict):
        try:
            result = callback(data) if not asyncio.iscoroutinefunction(callback) else await callback(data)
            return result
        except Exception as exc:
            logger.error("event_bus_callback_error", event=event, error=str(exc))
