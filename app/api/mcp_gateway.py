"""MCP Gateway HTTP API (TeleMon AI Platform Phase 1).

Exposes the gateway over REST so the AI Operations Center frontend can:
- ``GET  /api/mcp-gateway/catalog`` — list registered servers and their tools,
- ``POST /api/mcp-gateway/invoke``   — call a tool (with approval gating),
- ``POST /api/mcp-gateway/approve``  — approve a pending approval-gated call.

Auth mirrors the rest of the app (``require_api_key_or_admin``).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.deps import Identity, get_current_identity, require_api_key_or_admin
from app.services.mcp_gateway import gateway

router = APIRouter(prefix="/api/mcp-gateway", tags=["mcp-gateway"])


# ─── Schemas ─────────────────────────────────────────────────────────────────


class InvokeRequest(BaseModel):
    server_id: str
    tool_name: str
    arguments: dict = {}
    request_id: str | None = None
    approved: bool = False


class ApproveRequest(BaseModel):
    request_id: str


# ─── Endpoints ──────────────────────────────────────────────────────────────


@router.get("/catalog")
async def get_catalog(identity: Identity = Depends(get_current_identity)) -> dict:
    """List all registered MCP servers and their tools."""
    return gateway.catalog().to_dict()


@router.post("/invoke")
async def invoke_tool(
    payload: InvokeRequest,
    identity: Identity = Depends(get_current_identity),
) -> dict:
    """Invoke a tool on a registered MCP server.

    Approving a previously gated call: send the same ``request_id`` returned by
    the pending response with ``approved=True``.
    """
    request_id = payload.request_id
    approved = payload.approved

    if approved and request_id:
        pending = gateway.get_pending(request_id)
        if pending is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="pending approval not found (expired or already handled)",
            )
        result = await gateway.invoke(
            pending.server_id,
            pending.tool_name,
            pending.arguments,
            approved=True,
        )
        gateway.clear_pending(request_id)
        return result

    result = await gateway.invoke(
        payload.server_id,
        payload.tool_name,
        payload.arguments,
        approved=False,
    )

    # Surface HTTP 202 for a tool awaiting approval so the frontend can show a
    # confirm dialog rather than treating it as an error. The pending payload is
    # returned as the response body (not wrapped in `detail`).
    if result.get("pending_approval"):
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=result)
    if not result.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=result,
        )
    return result


@router.post("/approve")
async def approve_tool(
    payload: ApproveRequest,
    identity: Identity = Depends(get_current_identity),
) -> dict:
    """Approve and execute a previously gated tool call."""
    pending = gateway.get_pending(payload.request_id)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="pending approval not found (expired or already handled)",
        )
    result = await gateway.invoke(
        pending.server_id,
        pending.tool_name,
        pending.arguments,
        approved=True,
    )
    gateway.clear_pending(payload.request_id)
    return result
