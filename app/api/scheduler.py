from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import broadcast as broadcast_crud
from app.database import get_db
from app.schemas.broadcast import BroadcastRead

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.get("/upcoming", response_model=list[BroadcastRead])
async def read_upcoming(db: AsyncSession = Depends(get_db)):
    return await broadcast_crud.list_upcoming_scheduled_broadcasts(db)
