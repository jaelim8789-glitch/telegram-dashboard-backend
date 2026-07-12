from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_active_subscription
from app.crud import broadcast as broadcast_crud
from app.database import get_db
from app.schemas.broadcast import BroadcastRead

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"], dependencies=[Depends(require_active_subscription)])


@router.get("/upcoming", response_model=list[BroadcastRead])
async def read_upcoming(
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    # Scheduler shows upcoming broadcasts; tenant-isolated
    return await broadcast_crud.list_upcoming_scheduled_broadcasts(db, identity=identity)