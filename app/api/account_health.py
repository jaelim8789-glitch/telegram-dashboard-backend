"""Account Health API — tenant-isolated health monitoring with operational summary."""

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_identity, Identity, require_active_subscription
from app.services.account_health import get_account_health, HealthSummary, AccountHealthItem

router = APIRouter(prefix="/api/account-health", tags=["account-health"], dependencies=[Depends(require_active_subscription)])


@router.get("")
async def api_account_health(
    account_id: str | None = None,
    identity: Identity = Depends(get_current_identity),
):
    """Get detailed account health status for all authorized accounts.

    Derives health from Account model fields (status, session_data) and
    recent MessageLog delivery outcomes. Tenant-isolated.
    """
    result = await get_account_health(identity, account_id=account_id)
    return result
