"""Webhook settings API — CRUD for tenant webhook URLs."""

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_current_identity, require_tenant_access
from app.services.webhook_service import get_webhook_urls, set_webhook_urls

router = APIRouter(prefix="/api/webhook-settings", tags=["webhook"])


@router.get("/{tenant_id}")
async def list_webhook_urls(
    tenant_id: str,
    identity=Depends(get_current_identity),
):
    """Get webhook URLs for the tenant."""
    await require_tenant_access(tenant_id, identity)
    urls = await get_webhook_urls(tenant_id)
    return {"urls": urls}


@router.put("/{tenant_id}")
async def update_webhook_urls(
    tenant_id: str,
    body: dict,
    identity=Depends(get_current_identity),
):
    """Update webhook URLs for the tenant.

    Body: { "urls": ["https://hooks.slack.com/...", "https://example.com/webhook"] }
    """
    await require_tenant_access(tenant_id, identity)
    urls = body.get("urls", [])
    if not isinstance(urls, list):
        raise HTTPException(400, "\"urls\" must be a list of strings.")
    await set_webhook_urls(tenant_id, urls)
    return {"status": "saved", "urls": urls}


@router.post("/{tenant_id}/test")
async def test_webhook(
    tenant_id: str,
    body: dict,
    identity=Depends(get_current_identity),
):
    """Send a test webhook to a specific URL (does not save)."""
    await require_tenant_access(tenant_id, identity)
    url = body.get("url", "")
    if not url:
        raise HTTPException(400, "\"url\" is required.")
    from app.services.webhook_service import send_webhook
    count = await send_webhook(tenant_id, "broadcast.completed", {
        "broadcast_id": "__test__",
        "message_preview": "🔔 TeleMon 웹훅 테스트 알림입니다.",
        "success_count": 1,
        "failure_count": 0,
        "total_recipients": 1,
        "test": true,
    })
    if count > 0:
        return {"status": "delivered"}
    return {"status": "failed", "detail": "웹훅 전송에 실패했습니다. URL을 확인하세요."}
