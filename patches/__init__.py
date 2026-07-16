"""
Hot-patch: add debug logging to delivery pipeline.
Run inside backend-backend-1 container.
"""
import logging
logger = logging.getLogger(__name__)

from telethon.tl.types import ReplyInlineMarkup, KeyboardButtonUrl
import asyncio
from datetime import datetime, timezone
from app.services.delivery import DeliveryStatus, classify_error
from telethon.errors import FloodWaitError

async def patched_send_single(client, target, message, media_path,
                               reply_to_msg_id=None, inline_buttons=None):
    started = datetime.now(timezone.utc)
    logger.info(
        "DELIVERY_DEBUG: _send_single target=%s reply_to=%s msg_preview=%s",
        str(target), str(reply_to_msg_id), (message or "")[:80]
    )
    try:
        if media_path:
            send_coro = client.send_file(target, media_path, caption=message,
                                          reply_to=reply_to_msg_id)
        else:
            send_coro = client.send_message(target, message,
                                             reply_to=reply_to_msg_id)
        result = await asyncio.wait_for(send_coro, timeout=30.0)
        msg_id = result.id if hasattr(result, "id") else None
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        logger.info("DELIVERY_DEBUG: OK target=%s msg_id=%s elapsed=%.1fs reply_to=%s",
                     str(target), str(msg_id), elapsed, str(reply_to_msg_id))
        return (DeliveryStatus.SUCCESS, msg_id, None, None)
    except asyncio.TimeoutError:
        logger.warning("DELIVERY_DEBUG: timeout target=%s", str(target))
        return (DeliveryStatus.NETWORK_ERROR, None, "timeout", None)
    except Exception as exc:
        status, safe_error = classify_error(exc)
        fw = exc.seconds if isinstance(exc, FloodWaitError) else None
        logger.warning("DELIVERY_DEBUG: FAIL target=%s status=%s reply_to=%s err=%s",
                        str(target), status.value, str(reply_to_msg_id), str(exc)[:120])
        return (status, None, safe_error, fw)

def apply():
    import app.services.delivery as dmod
    dmod._send_single = patched_send_single
    logger.info("DELIVERY_DEBUG: patch applied")

if __name__ == "__main__":
    apply()
    print("Patch applied")