"""Wires the dispatcher's drained events to connected websocket clients.

This is the only consumer today; if a local dialogs/messages cache table is
added later, its sync function subscribes here too (dispatcher.subscribe
supports multiple consumers per event_type).
"""

from app.core.logging import get_logger
from app.realtime.dispatcher import UpdateEvent, dispatcher
from app.routes.ws import broadcast_to_account

logger = get_logger(__name__)


async def _forward_to_websocket(event: UpdateEvent) -> None:
    await broadcast_to_account(event.account_id, {
        "type": event.event_type,
        "chat_id": event.chat_id,
        **event.payload,
    })


def register_broadcast_consumer() -> None:
    dispatcher.subscribe("*", _forward_to_websocket)
