"""AI Providers API — manage external AI API provider configurations."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.models.ai_api import AiApiProviderConfig, AiApiCallLog
from app.ai.schemas.ai_api import (
    ApiProviderConfigCreate,
    ApiProviderConfigUpdate,
    ApiProviderConfigResponse,
    ApiCallRequest,
    ApiCallResponse,
    ApiCallLogResponse,
    ApiProviderListResponse,
)
from app.ai.api.provider import get_api_provider
from app.api.deps import get_current_tenant_id
from app.database import get_db

router = APIRouter(prefix="/ai/providers", tags=["AI Providers"])


@router.get("", response_model=ApiProviderListResponse)
async def list_providers(
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AiApiProviderConfig).where(AiApiProviderConfig.tenant_id == tenant_id)
    )
    providers = result.scalars().all()
    return ApiProviderListResponse(
        providers=[ApiProviderConfigResponse.model_validate(p) for p in providers],
        total=len(providers),
    )


@router.post("", response_model=ApiProviderConfigResponse, status_code=201)
async def create_provider(
    data: ApiProviderConfigCreate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    import uuid
    from app.core.crypto import encrypt

    provider = AiApiProviderConfig(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        provider_name=data.provider_name,
        api_base_url=data.api_base_url,
        api_key_encrypted=encrypt(data.api_key),
        model=data.model,
        max_tokens=data.max_tokens,
        temperature=int(data.temperature * 100),
        timeout_seconds=data.timeout_seconds,
        rate_limit_rpm=data.rate_limit_rpm,
        rate_limit_tpm=data.rate_limit_tpm,
        is_default=data.is_default,
        meta=data.meta,
    )
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    return ApiProviderConfigResponse.model_validate(provider)


@router.put("/{provider_id}", response_model=ApiProviderConfigResponse)
async def update_provider(
    provider_id: str,
    data: ApiProviderConfigUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AiApiProviderConfig).where(AiApiProviderConfig.id == provider_id)
    )
    provider = result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    update_data = data.model_dump(exclude_none=True)
    if "api_key" in update_data:
        from app.core.crypto import encrypt
        update_data["api_key_encrypted"] = encrypt(update_data.pop("api_key"))
    if "temperature" in update_data:
        update_data["temperature"] = int(update_data["temperature"] * 100)

    for key, value in update_data.items():
        if hasattr(provider, key) and value is not None:
            setattr(provider, key, value)

    await db.commit()
    await db.refresh(provider)
    return ApiProviderConfigResponse.model_validate(provider)


@router.delete("/{provider_id}", status_code=204)
async def delete_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AiApiProviderConfig).where(AiApiProviderConfig.id == provider_id)
    )
    provider = result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    await db.delete(provider)
    await db.commit()


@router.post("/chat", response_model=ApiCallResponse)
async def chat_completion(
    request: ApiCallRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    provider = get_api_provider()
    result = await provider.chat_completion(
        request.messages,
        provider=request.provider,
        model=request.model,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        tools=request.tools,
        stream=request.stream,
        db=db,
        tenant_id=tenant_id,
        correlation_id=request.correlation_id,
    )
    return ApiCallResponse(**result)