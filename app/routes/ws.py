import asyncio
import json
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from app.core.logging import get_logger

ws_router = APIRouter()
logger = get_logger(__name__)

# In-memory chat WebSocket connections: account_id → set of WebSocket clients
chat_clients: dict[str, set[WebSocket]] = {}


async def _validate_ws_identity(token: str | None, headers) -> "Identity | None":
    """Validate an auth token supplied on a WebSocket connection.

    Accepts the session token via the ``token`` query parameter or the
    X-Session-Token header, or a Bearer JWT via the Authorization header.
    Returns the resolved Identity, or None when no credentials were supplied
    or they could not be validated.
    """
    try:
        from app.api.deps import _resolve_identity
        from app.database import async_session_maker

        x_session_token = token or (headers.get("X-Session-Token") if headers else None)
        authorization = headers.get("Authorization") if headers else None
        async with async_session_maker() as db:
            return await _resolve_identity(None, authorization, x_session_token, db)
    except Exception:
        return None


async def _ws_auth_gate(websocket: WebSocket, token: str | None) -> "Identity | None":
    """Hard-enforcement auth gate for WebSocket connections.

    Every /ws/* connection must present valid credentials (via the token query
    param, X-Session-Token header, or Authorization Bearer JWT). Missing or
    invalid credentials close the connection with 4401 (unauthorized). Returns
    the resolved Identity when authorized, or None when the connection has been
    closed and the handler must return.
    """
    identity = await _validate_ws_identity(token, websocket.headers)
    if identity is not None:
        return identity

    supplied = bool(
        token
        or websocket.headers.get("X-Session-Token")
        or websocket.headers.get("Authorization")
    )
    if not supplied:
        logger.warning("ws_auth_missing_token", path=websocket.url.path)
    else:
        logger.warning("ws_auth_rejected", path=websocket.url.path)
    await websocket.accept()
    await websocket.close(code=4401)
    return None


async def _verify_account_access(
    websocket: WebSocket,
    identity: "Identity",
    account_id: str | None,
) -> bool:
    """Verify the authenticated identity's tenant owns the given account.

    Mirrors the REST-side ``require_account_tenant_access`` logic for WebSocket
    subscriptions. Returns True when access is allowed; otherwise closes the
    connection with 4401 and returns False. Admin identities may subscribe to
    any account. Users without a tenant are denied.
    """
    if account_id is None:
        logger.warning("ws_account_denied_no_account", path=websocket.url.path)
        await websocket.accept()
        await websocket.close(code=4401)
        return False

    if identity.kind == "admin":
        return True

    if identity.tenant_id is None:
        logger.warning(
            "ws_account_denied_no_tenant",
            account_id=account_id,
            identity_kind=identity.kind,
        )
        await websocket.accept()
        await websocket.close(code=4401)
        return False

    try:
        from app.crud import account as account_crud
        from app.database import async_session_maker

        async with async_session_maker() as db:
            account = await account_crud.get_account(db, account_id)
    except Exception:
        account = None

    if account is None or account.tenant_id != identity.tenant_id:
        logger.warning(
            "ws_account_denied_cross_tenant",
            account_id=account_id,
            identity_kind=identity.kind,
        )
        await websocket.accept()
        await websocket.close(code=4401)
        return False

    return True


async def broadcast_to_account(account_id: str, message: dict) -> None:
    """Send a JSON message to every open /ws/chat connection for this account.
    Used by app/realtime to push live Telegram updates without the frontend polling."""
    dead: list[WebSocket] = []
    for client in chat_clients.get(account_id, set()):
        try:
            await client.send_json(message)
        except Exception:
            dead.append(client)
    for client in dead:
        chat_clients.get(account_id, set()).discard(client)


async def broadcast_dialog_update(account_id: str) -> None:
    """Send a dialog_update event to every open /ws/chat connection for this account."""
    dead: list[WebSocket] = []
    for client in chat_clients.get(account_id, set()):
        try:
            await client.send_json({"type": "dialog_update"})
        except Exception:
            dead.append(client)
    for client in dead:
        chat_clients.get(account_id, set()).discard(client)


@ws_router.websocket("/ws/chat")
async def chat_websocket(
    websocket: WebSocket,
    account_id: str = Query(...),
    token: Optional[str] = Query(default=None),
):
    identity = await _ws_auth_gate(websocket, token)
    if identity is None:
        return
    # Tenant ownership check: an authenticated user may only subscribe to
    # accounts their own tenant owns (prevents cross-tenant IDOR).
    if not await _verify_account_access(websocket, identity, account_id):
        return
    await websocket.accept()
    if account_id not in chat_clients:
        chat_clients[account_id] = set()
    chat_clients[account_id].add(websocket)

    async def send_broadcast_events():
        while True:
            try:
                from app.api.chats import pop_ws_events
                events = pop_ws_events()
                for event in events:
                    stale: list[WebSocket] = []
                    for client in chat_clients.get(account_id, set()):
                        try:
                            await client.send_json(event)
                        except WebSocketDisconnect:
                            stale.append(client)
                    for s in stale:
                        chat_clients[account_id].discard(s)
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break

    broadcast_task = asyncio.create_task(send_broadcast_events())
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
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
    finally:
        broadcast_task.cancel()
        try:
            await broadcast_task
        except asyncio.CancelledError:
            pass


@ws_router.websocket("/ws/dashboard")
async def dashboard_websocket(
    websocket: WebSocket,
    account_id: Optional[str] = Query(None),
    token: Optional[str] = Query(default=None),
):
    identity = await _ws_auth_gate(websocket, token)
    if identity is None:
        return
    # account_id is required — omitting it would surface all accounts' stats.
    if not await _verify_account_access(websocket, identity, account_id):
        return
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
async def sessions_websocket(
    websocket: WebSocket,
    token: Optional[str] = Query(default=None),
):
    """Real-time session state updates over WebSocket."""
    identity = await _ws_auth_gate(websocket, token)
    if identity is None:
        return
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
                # Only allow reconnecting accounts the tenant owns.
                if account_id and await _verify_account_access(websocket, identity, account_id) and manager._initialized:
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
