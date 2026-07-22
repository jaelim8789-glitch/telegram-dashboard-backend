import asyncio
import json
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

ws_router = APIRouter()


@ws_router.websocket("/ws/dashboard")
async def dashboard_websocket(
    websocket: WebSocket,
    account_id: Optional[str] = Query(None),
):
    await websocket.accept()

    async def send_periodic_stats():
        while True:
            try:
                stats = await collect_dashboard_stats(account_id)
                await websocket.send_json(stats)
                await asyncio.sleep(5)
            except WebSocketDisconnect:
                break
            except Exception:
                break

    sender_task = asyncio.create_task(send_periodic_stats())

    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        sender_task.cancel()
        try:
            await sender_task
        except asyncio.CancelledError:
            pass


async def collect_dashboard_stats(account_id: Optional[str] = None) -> dict:
    from app.api.logs import router as logs_router
    from app.api.scheduler import router as scheduler_router
    from app.api.telemon_memory import router as telemon_memory_router

    data = {"type": "stats_update"}

    try:
        from app.services.dashboard import get_overview, get_health
        data["overview"] = await get_overview(account_id)
        data["health"] = await get_health(account_id)
    except Exception:
        data["overview"] = {}
        data["health"] = {}

    try:
        from app.services.dashboard import get_recent_logs
        data["recent_logs"] = await get_recent_logs(account_id, limit=10)
    except Exception:
        data["recent_logs"] = []

    try:
        from app.services.dashboard import get_scheduler_status
        data["scheduler"] = await get_scheduler_status()
    except Exception:
        data["scheduler"] = {}

    try:
        from app.services.dashboard import get_telememory_snapshot
        data["telememory"] = await get_telememory_snapshot(account_id)
    except Exception:
        data["telememory"] = {}

    return data
