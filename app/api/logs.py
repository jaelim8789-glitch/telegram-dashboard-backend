from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_account_tenant_access
from app.crud import account as account_crud
from app.crud import broadcast as broadcast_crud
from app.database import get_db
from app.schemas.broadcast import BroadcastRead

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("", response_model=list[BroadcastRead])
async def read_logs(
    account_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    date: str | None = Query(default=None, description="YYYY-MM-DD"),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    # Ownership is only checked when an account_id filter is given — that dependency
    # requires an account_id (404s if it doesn't resolve to a real Account), so it
    # can't run as a blanket Depends() here without breaking the "all logs" case
    # (account_id omitted). When account_id is omitted, list_logs itself already
    # scopes the query to the caller's tenant (see app/crud/broadcast.py), so
    # tenant isolation still holds without this extra check.
    if account_id:
        await require_account_tenant_access(account_id=account_id, db=db, identity=identity)
    return await broadcast_crud.list_logs(db, identity=identity, account_id=account_id, status=status, date=date)