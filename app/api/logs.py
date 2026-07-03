from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

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
):
    return await broadcast_crud.list_logs(db, account_id=account_id, status=status, date=date)
