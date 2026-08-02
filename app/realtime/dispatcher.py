"""Central dispatcher for Telethon updates across all pooled accounts.

Telethon invokes event callbacks directly on the client's own network loop
iteration. Doing real work there (DB writes, websocket sends) means a slow
consumer stalls that account's connection and can cascade into missed reads /
flood-wait accumulation once dozens of accounts are live at once.

Instead, every registered handler in app/realtime/handlers.py does the
minimum needed to build a small dict (`UpdateEvent`) and hands it off here
with `publish()`, which is a non-blocking queue put. A small fixed pool of
worker tasks drains the queue and runs the actual consumers (websocket
broadcast today; DB sync later if a local cache table is added).
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.core.logging import get_logger

logger = get_logger(__name__)

# Bounded so a burst (e.g. an account added to a busy broadcast group) can't
# grow memory unboundedly; publish() drops and logs rather than blocking the
# Telethon callback when full.
QUEUE_MAXSIZE = 4000
WORKER_COUNT = 4

Consumer = Callable[["UpdateEvent"], Awaitable[None]]


@dataclass(frozen=True)
class UpdateEvent:
    account_id: str
    event_type: str  # "message.new" | "message.edited" | "message.deleted" |
                      # "message.read" | "user.status" | "chat.typing" | "chat.action"
    chat_id: int | None
    payload: dict[str, Any] = field(default_factory=dict)


class UpdateDispatcher:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[UpdateEvent] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._consumers: dict[str, list[Consumer]] = {}
        self._workers: list[asyncio.Task] = []
        self._dropped_count = 0

    def subscribe(self, event_type: str, consumer: Consumer) -> None:
        """Register a consumer for one event type, or "*" for all types."""
        self._consumers.setdefault(event_type, []).append(consumer)

    def publish(self, event: UpdateEvent) -> None:
        """Non-blocking. Called from inside Telethon event callbacks — never await here."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped_count += 1
            if self._dropped_count % 100 == 1:
                logger.warning(
                    "realtime_queue_full_dropping_event",
                    account_id=event.account_id,
                    event_type=event.event_type,
                    total_dropped=self._dropped_count,
                )

    async def _worker(self, worker_id: int) -> None:
        while True:
            event = await self._queue.get()
            try:
                consumers = self._consumers.get(event.event_type, []) + self._consumers.get("*", [])
                for consumer in consumers:
                    try:
                        await consumer(event)
                    except Exception as exc:
                        logger.error(
                            "realtime_consumer_failed",
                            worker_id=worker_id,
                            event_type=event.event_type,
                            account_id=event.account_id,
                            error=str(exc),
                        )
            finally:
                self._queue.task_done()

    def start(self) -> None:
        if self._workers:
            return
        self._workers = [
            asyncio.create_task(self._worker(i)) for i in range(WORKER_COUNT)
        ]
        logger.info("realtime_dispatcher_started", worker_count=WORKER_COUNT)

    async def stop(self) -> None:
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._workers = []
        logger.info("realtime_dispatcher_stopped")

    def stats(self) -> dict:
        return {
            "queue_size": self._queue.qsize(),
            "queue_maxsize": QUEUE_MAXSIZE,
            "worker_count": len(self._workers),
            "dropped_total": self._dropped_count,
        }


dispatcher = UpdateDispatcher()
