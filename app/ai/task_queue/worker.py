"""
Task Worker — processes tasks from the queue with error isolation.

Runs in a background asyncio loop, picking up tasks and dispatching them
to appropriate handlers based on task_type.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.config import get_ai_config
from app.ai.task_queue.queue import get_task_queue
from app.database import async_session_maker
from app.core.logging import get_logger

logger = get_logger(__name__)

# Task handler type
TaskHandler = Callable[..., Any]


class TaskWorker:
    """Background worker that processes tasks from the queue."""

    def __init__(self) -> None:
        self._queue = get_task_queue()
        self._config = get_ai_config()
        self._handlers: dict[str, TaskHandler] = {}
        self._running = False
        self._task: asyncio.Task[None] | None = None

    def register_handler(self, task_type: str, handler: TaskHandler) -> None:
        """Register a handler for a specific task type."""
        self._handlers[task_type] = handler
        logger.debug("task_handler_registered", task_type=task_type)

    def unregister_handler(self, task_type: str) -> None:
        """Unregister a handler."""
        self._handlers.pop(task_type, None)

    def start(self) -> None:
        """Start the background worker loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._worker_loop())
        logger.info("task_worker_started")

    async def stop(self) -> None:
        """Stop the background worker loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("task_worker_stopped")

    async def _worker_loop(self) -> None:
        """Main worker loop — polls the queue and processes tasks."""
        while self._running:
            try:
                async with async_session_maker() as db:
                    task = await self._queue.dequeue(db)
                    if task:
                        await self._process_task(db, task)
            except Exception as exc:
                logger.error("task_worker_loop_error", error=str(exc))

            await asyncio.sleep(self._config.task_queue_poll_interval)

    async def _process_task(self, db: AsyncSession, task: Any) -> None:
        """Process a single task with error isolation."""
        task_id = task.id
        tenant_id = task.tenant_id
        task_type = task.task_type

        # Claim the task
        claimed = await self._queue.claim(db, task_id)
        if not claimed:
            return

        logger.info("task_processing", task_id=task_id, task_type=task_type)

        try:
            handler = self._handlers.get(task_type)
            if not handler:
                raise ValueError(f"No handler registered for task type '{task_type}'")

            # Add task log
            await self._queue.add_log(db, task_id, tenant_id, "info", f"Processing task: {task_type}")

            # Execute handler
            start_time = time.monotonic()
            result = await handler(db, tenant_id, task.payload)
            duration_ms = int((time.monotonic() - start_time) * 1000)

            # Complete task
            await self._queue.complete(db, task_id, result=result, status="completed")
            await self._queue.add_log(
                db, task_id, tenant_id, "info",
                f"Task completed in {duration_ms}ms",
                {"duration_ms": duration_ms},
            )
            logger.info("task_completed", task_id=task_id, duration_ms=duration_ms)

        except Exception as exc:
            error_msg = str(exc)
            logger.error("task_failed", task_id=task_id, error=error_msg)

            await self._queue.add_log(
                db, task_id, tenant_id, "error",
                f"Task failed: {error_msg}",
            )

            # Retry or fail
            await self._queue.retry(db, task_id)


# ── Singleton ─────────────────────────────────────────────────────────

_worker: TaskWorker | None = None


def get_task_worker() -> TaskWorker:
    """Get the singleton task worker instance."""
    global _worker
    if _worker is None:
        _worker = TaskWorker()
    return _worker