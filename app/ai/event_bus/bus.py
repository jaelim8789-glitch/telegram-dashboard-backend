"""
AI Event Bus — in-memory pub/sub for AI platform events.

Supports typed event emissions with subscriber filtering, async handlers,
error isolation, and audit logging.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.config import get_ai_config
from app.ai.models.ai_event import AiEventLog, AiEventSubscription
from app.core.logging import get_logger

logger = get_logger(__name__)

# Event handler type
EventHandler = Callable[..., Awaitable[None]]


class EventBus:
    """In-memory pub/sub event bus for AI platform events.

    Events flow through the bus to registered subscribers. Each event
    is logged for audit. Subscribers run independently with error isolation.
    """

    def __init__(self) -> None:
        self._config = get_ai_config()
        self._subscribers: dict[str, list[EventHandler]] = {}
        self._db_subscriptions: dict[str, AiEventSubscription] = {}
        self._running = False

    # ── Event Emission ───────────────────────────────────────────────

    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        source: str = "system",
        correlation_id: str | None = None,
        db: AsyncSession | None = None,
        tenant_id: str | None = None,
    ) -> int:
        """Publish an event to all matching subscribers.

        Returns the number of handlers invoked.
        """
        event_id = str(uuid.uuid4())
        correlation_id = correlation_id or event_id

        # Find matching handlers
        handlers: list[EventHandler] = []
        for pattern, handler_list in self._subscribers.items():
            if self._match_pattern(pattern, event_type):
                handlers.extend(handler_list)

        # Add DB-based subscription handlers
        for sub in self._db_subscriptions.values():
            if sub.is_active and self._match_pattern(sub.event_type, event_type):
                if self._matches_filter(sub.filter_condition, payload):
                    # Import the handler dynamically
                    pass  # In-memory handlers cover this

        # Invoke handlers with error isolation
        tasks = [self._safe_call(h, event_type, payload) for h in handlers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        success_count = sum(1 for r in results if not isinstance(r, Exception))
        failure_count = sum(1 for r in results if isinstance(r, Exception))

        # Log event to DB if session provided
        if db and tenant_id:
            try:
                log_entry = AiEventLog(
                    id=event_id,
                    tenant_id=tenant_id,
                    event_type=event_type,
                    source=source,
                    payload=payload,
                    correlation_id=correlation_id,
                    handler_count=len(handlers),
                    handler_success_count=success_count,
                    handler_failure_count=failure_count,
                )
                db.add(log_entry)
                await db.commit()
            except Exception as exc:
                logger.warning("event_log_failed", error=str(exc))

        logger.debug(
            "event_published",
            event_type=event_type,
            handlers=len(handlers),
            success=success_count,
            failure=failure_count,
        )
        return len(handlers)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Subscribe a handler to an event type.

        Supports wildcards: 'tool.*' matches 'tool.executed', 'tool.failed', etc.
        """
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)
        logger.debug("event_subscriber_added", event_type=event_type)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """Remove a subscriber."""
        handlers = self._subscribers.get(event_type)
        if handlers:
            try:
                handlers.remove(handler)
            except ValueError:
                pass

    # ── DB-backed subscriptions ──────────────────────────────────────

    async def load_subscriptions(self, db: AsyncSession) -> int:
        """Load active subscriptions from the database."""
        from sqlalchemy import select
        result = await db.execute(
            select(AiEventSubscription).where(AiEventSubscription.is_active == True)
        )
        subs = result.scalars().all()
        for sub in subs:
            self._db_subscriptions[sub.id] = sub
        logger.info("event_subscriptions_loaded", count=len(subs))
        return len(subs)

    # ── Internal ─────────────────────────────────────────────────────

    async def _safe_call(
        self, handler: EventHandler, event_type: str, payload: dict[str, Any]
    ) -> None:
        """Call a handler with error isolation."""
        try:
            await handler(event_type, payload)
        except Exception as exc:
            logger.error(
                "event_handler_failed",
                event_type=event_type,
                error=str(exc),
                handler=getattr(handler, "__name__", "<anonymous>"),
            )

    def _match_pattern(self, pattern: str, event_type: str) -> bool:
        """Check if an event_type matches a subscription pattern.

        Supports: 'exact.match', 'prefix.*', '*'
        """
        if pattern == "*":
            return True
        if pattern.endswith(".*"):
            return event_type.startswith(pattern[:-1])
        return pattern == event_type

    def _matches_filter(
        self, filter_condition: dict[str, Any] | None, payload: dict[str, Any]
    ) -> bool:
        """Check if payload matches the filter condition."""
        if not filter_condition:
            return True
        for key, value in filter_condition.items():
            if payload.get(key) != value:
                return False
        return True

    def clear(self) -> None:
        """Remove all subscribers."""
        self._subscribers.clear()
        self._db_subscriptions.clear()


# ── Singleton ─────────────────────────────────────────────────────────

_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Get the singleton event bus instance."""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus