"""Billing / 결제 API — USDT + Telegram Stars only.

Plan data sourced from canonical app.core.plans.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_identity, Identity, require_admin, require_tenant_access
from app.core.logging import get_logger
from app.core.plans import PLAN_CATALOG, validate_plan_id
from app.services.billing import (
    cancel_subscription,
    confirm_usdt_payment,
    create_stars_invoice,
    create_usdt_invoice,
    get_all_addons,
    get_subscription_status,
    process_stars_payment,
)
from app.services.usage_tracker import add_stars_credit

router = APIRouter(prefix="/api/billing", tags=["billing"])
logger = get_logger(__name__)


# ─── Plans ────────────────────────────────────────────────────────────


@router.get("/plans")
async def api_get_plans():
    """Get all plan info with USDT prices from canonical PLAN_CATALOG."""
    plans = []
    for plan_id, pdef in PLAN_CATALOG.items():
        entry = {
            "id": plan_id,
            "name": pdef["name"],
            "description": pdef["description"],
            "features": pdef["features"],
        }
        for interval, price in pdef["prices_usdt"].items():
            entry["price_usdt"] = price
            entry["billing_interval"] = interval
            entry["billing_label"] = "분기" if interval == "quarterly" else "월"
        plans.append(entry)

    return {
        "plans": plans,
        "addons": get_all_addons(),
        "payment_methods": ["usdt", "stars"],
    }


# ─── USDT Payment ─────────────────────────────────────────────────────


@router.post("/usdt/invoice")
async def api_create_usdt_invoice(
    tenant_id: str,
    plan: str,
    billing: str = "monthly",
    identity: Identity = Depends(get_current_identity),
):
    """Create a USDT payment invoice."""
    await require_tenant_access(tenant_id, identity)
    try:
        validate_plan_id(plan)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if billing not in ("monthly", "quarterly"):
        raise HTTPException(status_code=400, detail="billing은 monthly 또는 quarterly이어야 합니다.")
    plan_def = PLAN_CATALOG.get(plan)
    if plan_def and billing not in plan_def["prices_usdt"]:
        available = ", ".join(plan_def["prices_usdt"].keys())
        raise HTTPException(
            status_code=400,
            detail=f"'{plan}' 요금제는 {available} 결제 주기를 지원합니다. '{billing}'은(는) 지원되지 않습니다.",
        )
    result = await create_usdt_invoice(tenant_id, plan, billing)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "인보이스 생성 실패"))
    return result


@router.post("/usdt/confirm", dependencies=[Depends(require_admin)])
async def api_confirm_usdt_payment(
    tenant_id: str,
    tx_hash: str,
    identity: Identity = Depends(get_current_identity),
):
    """Confirm USDT payment (admin only)."""
    await require_tenant_access(tenant_id, identity)
    result = await confirm_usdt_payment(tenant_id, tx_hash)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "결제 확인 실패"))
    return result


# ─── Stars Payment ────────────────────────────────────────────────────


@router.get("/stars/invoice/{item}")
async def api_get_stars_invoice(item: str):
    """Get Stars price info for an add-on item."""
    result = await create_stars_invoice("", item)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail="유효하지 않은 아이템입니다.")
    return result


@router.post("/stars/add", dependencies=[Depends(require_admin)])
async def api_add_stars(
    tenant_id: str,
    stars_amount: int,
    identity: Identity = Depends(get_current_identity),
):
    """Add Stars to wallet (admin only until real payment verification exists)."""
    await require_tenant_access(tenant_id, identity)
    result = await add_stars_credit(tenant_id, stars_amount)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Stars 충전 실패"))
    return result


@router.post("/stars/spend")
async def api_spend_stars(
    tenant_id: str,
    item: str,
    identity: Identity = Depends(get_current_identity),
):
    """Spend Stars on an add-on item."""
    await require_tenant_access(tenant_id, identity)
    from app.services.billing import STARS_PRICES
    stars_amount = STARS_PRICES.get(item)
    if not stars_amount:
        raise HTTPException(status_code=404, detail="유효하지 않은 아이템입니다.")
    result = await process_stars_payment(tenant_id, item, stars_amount)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Stars 사용 실패"))
    return result


# ─── Subscription ─────────────────────────────────────────────────────


@router.get("/subscription/{tenant_id}")
async def api_get_subscription(
    tenant_id: str,
    identity: Identity = Depends(get_current_identity),
):
    """Get current subscription status."""
    await require_tenant_access(tenant_id, identity)
    return await get_subscription_status(tenant_id)


@router.post("/subscription/{tenant_id}/cancel")
async def api_cancel_subscription(
    tenant_id: str,
    identity: Identity = Depends(get_current_identity),
):
    """Cancel subscription."""
    await require_tenant_access(tenant_id, identity)
    result = await cancel_subscription(tenant_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "구독 취소 실패"))
    return result
