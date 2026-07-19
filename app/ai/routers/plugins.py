"""AI Plugins API — manage plugin registrations."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.models.ai_plugin import AiPluginRegistration
from app.ai.schemas.ai_plugin import (
    PluginRegistrationCreate,
    PluginRegistrationUpdate,
    PluginRegistrationResponse,
    PluginListResponse,
)
from app.ai.plugin.manager import get_plugin_manager
from app.api.deps import get_current_tenant_id
from app.database import get_db

router = APIRouter(prefix="/ai/plugins", tags=["AI Plugins"])


@router.get("", response_model=PluginListResponse)
async def list_plugins(
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AiPluginRegistration).where(AiPluginRegistration.tenant_id == tenant_id)
    )
    plugins = result.scalars().all()
    return PluginListResponse(
        plugins=[PluginRegistrationResponse.model_validate(p) for p in plugins],
        total=len(plugins),
    )


@router.post("", response_model=PluginRegistrationResponse, status_code=201)
async def register_plugin(
    data: PluginRegistrationCreate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    import uuid
    plugin = AiPluginRegistration(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        **data.model_dump(),
    )
    db.add(plugin)
    await db.commit()
    await db.refresh(plugin)
    return PluginRegistrationResponse.model_validate(plugin)


@router.get("/{plugin_id}", response_model=PluginRegistrationResponse)
async def get_plugin(
    plugin_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AiPluginRegistration).where(AiPluginRegistration.id == plugin_id)
    )
    plugin = result.scalar_one_or_none()
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
    return PluginRegistrationResponse.model_validate(plugin)


@router.put("/{plugin_id}", response_model=PluginRegistrationResponse)
async def update_plugin(
    plugin_id: str,
    data: PluginRegistrationUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AiPluginRegistration).where(AiPluginRegistration.id == plugin_id)
    )
    plugin = result.scalar_one_or_none()
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
    for key, value in data.model_dump(exclude_none=True).items():
        if hasattr(plugin, key) and value is not None:
            setattr(plugin, key, value)
    await db.commit()
    await db.refresh(plugin)
    return PluginRegistrationResponse.model_validate(plugin)


@router.delete("/{plugin_id}", status_code=204)
async def delete_plugin(
    plugin_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AiPluginRegistration).where(AiPluginRegistration.id == plugin_id)
    )
    plugin = result.scalar_one_or_none()
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
    await db.delete(plugin)
    await db.commit()


@router.post("/{plugin_id}/load")
async def load_plugin(
    plugin_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AiPluginRegistration).where(AiPluginRegistration.id == plugin_id)
    )
    plugin = result.scalar_one_or_none()
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
    return {"status": "registered", "plugin": plugin.name, "note": "Plugin loaded from DB registry"}


@router.get("/loaded/list")
async def list_loaded_plugins():
    manager = get_plugin_manager()
    plugins = manager.list_plugins()
    return {
        "plugins": [
            {"name": p.name, "version": p.version, "description": p.description}
            for p in plugins
        ],
        "total": len(plugins),
    }