import asyncio
import json
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

ws_router = APIRouter()

# In-memory chat WebSocket connections: account_id → set of WebSocket clients
chat_clients: dict[str, set[WebSocket]] = {}


@ws_router.websocket("/ws/chat")
async def chat_websocket(
    websocket: WebSocket,
    account_id: str = Query(...),
):
    await websocket.accept()
    if account_id not in chat_clients:
        chat_clients[account_id] = set()
    chat_clients[account_id].add(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            # Broadcast typing/read receipts to other clients of same account
            if msg.get("type") == "typing":
                for client in chat_clients.get(account_id, set()):
                    if client != websocket:
                        try:
                            await client.send_json(msg)
                        except WebSocketDisconnect:
                            chat_clients[account_id].discard(client)
    except WebSocketDisconnect:
        chat_clients[account_id].discard(websocket)
        if not chat_clients[account_id]:
            del chat_clients[account_id]


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


session_clients: set[WebSocket] = set()


@ws_router.websocket("/ws/sessions")
async def sessions_websocket(websocket: WebSocket):
    """Real-time session state updates over WebSocket."""
    await websocket.accept()
    session_clients.add(websocket)

    try:
        from app.services.session_manager import SessionManager
        manager = SessionManager()
        if manager._initialized:
            states = {}
            for aid, state in manager._connection_service._states.items():
                states[aid] = state.value
            await websocket.send_json({"type": "session_states", "states": states})
        else:
            await websocket.send_json({"type": "session_states", "states": {}})

        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
            elif msg.get("type") == "reconnect":
                account_id = msg.get("account_id")
                if account_id and manager._initialized:
                    import asyncio as _aio
                    _aio.create_task(manager.reconnect(account_id))
    except WebSocketDisconnect:
        session_clients.discard(websocket)
    except Exception:
        session_clients.discard(websocket)


async def broadcast_session_event(data: dict):
    """Push session event to all connected /ws/sessions clients."""
    payload = {"type": "session_event", **data}
    stale = set()
    for client in session_clients:
        try:
            await client.send_json(payload)
        except Exception:
            stale.add(client)
    session_clients.difference_update(stale)


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
