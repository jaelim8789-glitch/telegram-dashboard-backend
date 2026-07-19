"""AI Schedules API — manage scheduled job definitions and view execution history."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.models.ai_schedule import AiScheduleDefinition, AiScheduleExecution
from app.ai.schemas.ai_schedule import (
    ScheduleDefinitionCreate,
    ScheduleDefinitionUpdate,
    ScheduleDefinitionResponse,
    ScheduleExecutionResponse,
    ScheduleListResponse,
)
from app.ai.scheduler.service import get_ai_scheduler_service
from app.api.deps import get_current_tenant_id
from app.database import get_db

router = APIRouter(prefix="/ai/schedules", tags=["AI Schedules"])


@router.get("", response_model=ScheduleListResponse)
async def list_schedules(
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    service = get_ai_scheduler_service()
    schedules = await service.list_schedules(db, tenant_id)
    return ScheduleListResponse(
        schedules=[ScheduleDefinitionResponse.model_validate(s) for s in schedules],
        total=len(schedules),
    )


@router.post("", response_model=ScheduleDefinitionResponse, status_code=201)
async def create_schedule(
    data: ScheduleDefinitionCreate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    service = get_ai_scheduler_service()
    schedule = await service.create_schedule(db, tenant_id, data.model_dump())
    return ScheduleDefinitionResponse.model_validate(schedule)


@router.get("/{schedule_id}", response_model=ScheduleDefinitionResponse)
async def get_schedule(
    schedule_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AiScheduleDefinition).where(AiScheduleDefinition.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return ScheduleDefinitionResponse.model_validate(schedule)


@router.put("/{schedule_id}", response_model=ScheduleDefinitionResponse)
async def update_schedule(
    schedule_id: str,
    data: ScheduleDefinitionUpdate,
    db: AsyncSession = Depends(get_db),
):
    service = get_ai_scheduler_service()
    schedule = await service.update_schedule(db, schedule_id, data.model_dump(exclude_none=True))
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return ScheduleDefinitionResponse.model_validate(schedule)


@router.delete("/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: str,
    db: AsyncSession = Depends(get_db),
):
    service = get_ai_scheduler_service()
    deleted = await service.delete_schedule(db, schedule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Schedule not found")


@router.get("/{schedule_id}/executions", response_model=list[ScheduleExecutionResponse])
async def get_schedule_executions(
    schedule_id: str,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AiScheduleExecution)
        .where(AiScheduleExecution.schedule_id == schedule_id)
        .order_by(desc(AiScheduleExecution.triggered_at))
        .limit(limit)
    )
    executions = result.scalars().all()
    return [ScheduleExecutionResponse.model_validate(e) for e in executions]


@router.post("/{schedule_id}/trigger", response_model=ScheduleExecutionResponse)
async def trigger_schedule(
    schedule_id: str,
    db: AsyncSession = Depends(get_db),
):
    service = get_ai_scheduler_service()
    result = await db.execute(
        select(AiScheduleDefinition).where(AiScheduleDefinition.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    execution = await service.execute_schedule(db, schedule)
    return ScheduleExecutionResponse.model_validate(execution)