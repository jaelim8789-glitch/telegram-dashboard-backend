from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Identity, get_current_identity
from app.database import get_db
from app.services.telemon_memory_service import build_telemon_memory_snapshot

router = APIRouter(prefix="/api/ai/telemon-memory", tags=["telemon-memory"])


@router.get("/snapshot")
async def get_telemon_memory_snapshot(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    if identity.tenant_id is None and identity.kind != "admin":
        raise HTTPException(status_code=403, detail="tenant_id is required")
    return await build_telemon_memory_snapshot(db, identity)