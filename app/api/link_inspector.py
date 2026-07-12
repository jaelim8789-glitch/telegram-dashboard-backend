from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_account_tenant_access
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.database import get_db
from app.schemas.link_inspector import (
    JoinLinksRequest,
    LinkInspectRequest,
    LinkInspectResponse,
    LinkJoinResponse,
)
from app.services.link_inspector_service import (
    DailyJoinLimitExceededError,
    inspect_links,
    join_selected_links,
)
from app.services.telegram_actions import AccountNotAuthenticatedError

router = APIRouter(prefix="/api/link-inspector", tags=["link-inspector"])
logger = get_logger(__name__)


@router.post("/inspect", response_model=LinkInspectResponse)
async def inspect(
    payload: LinkInspectRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Bulk-inspect pasted t.me links/usernames using the account's Telethon session."""
    await require_account_tenant_access(payload.account_id, db, identity)
    account = await account_crud.get_account(db, payload.account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    try:
        items, duplicates_removed = await inspect_links(account, payload.links)
    except AccountNotAuthenticatedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        logger.error("link_inspect_failed", account_id=account.id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"링크 검사에 실패했습니다: {exc}",
        )

    return LinkInspectResponse(
        items=items,
        total_submitted=len(payload.links),
        duplicates_removed=duplicates_removed,
        total_inspected=len(items),
    )


@router.post("/join", response_model=LinkJoinResponse)
async def join(
    payload: JoinLinksRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Join selected active links, adding them to the account's Group Management list."""
    await require_account_tenant_access(payload.account_id, db, identity)
    account = await account_crud.get_account(db, payload.account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    try:
        results = await join_selected_links(account, payload.targets)
    except AccountNotAuthenticatedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except DailyJoinLimitExceededError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))
    except Exception as exc:
        logger.error("link_inspector_join_failed", account_id=account.id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"그룹 가입에 실패했습니다: {exc}",
        )

    return LinkJoinResponse(items=results)
