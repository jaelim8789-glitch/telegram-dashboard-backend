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
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    if account_id:
        await require_account_tenant_access(account_id=account_id, db=db, identity=identity)
    return await broadcast_crud.list_logs(
        db, identity=identity, account_id=account_id, status=status, date=date,
        page=page, limit=limit,
    )