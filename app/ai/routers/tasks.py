"""AI Tasks API — manage task queue and view task status."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.models.ai_task import AiTask
from app.ai.schemas.ai_task import (
    TaskCreate,
    TaskResponse,
    TaskListResponse,
    TaskQueueStats,
)
from app.ai.task_queue.queue import get_task_queue
from app.api.deps import get_current_tenant_id
from app.database import get_db

router = APIRouter(prefix="/ai/tasks", tags=["AI Tasks"])


@router.post("", response_model=TaskResponse, status_code=201)
async def create_task(
    data: TaskCreate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    queue = get_task_queue()
    task = await queue.enqueue(
        db, tenant_id, data.task_type, data.payload,
        priority=data.priority,
        max_retries=data.max_retries,
        schedule_at=data.schedule_at,
    )
    return TaskResponse.model_validate(task)


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    status: str | None = None,
    task_type: str | None = None,
    limit: int = 50,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    query = select(AiTask).where(AiTask.tenant_id == tenant_id)
    if status:
        query = query.where(AiTask.status == status)
    if task_type:
        query = query.where(AiTask.task_type == task_type)
    query = query.order_by(AiTask.created_at.desc()).limit(limit)

    result = await db.execute(query)
    tasks = result.scalars().all()

    # Counts
    count_query = select(AiTask.status, func.count(AiTask.id)).where(
        AiTask.tenant_id == tenant_id
    ).group_by(AiTask.status)
    count_result = await db.execute(count_query)
    counts = {row.status: row[1] for row in count_result}

    return TaskListResponse(
        tasks=[TaskResponse.model_validate(t) for t in tasks],
        total=len(tasks),
        pending=counts.get("pending", 0),
        running=counts.get("running", 0),
        completed=counts.get("completed", 0),
        failed=counts.get("failed", 0),
    )


@router.get("/stats", response_model=TaskQueueStats)
async def get_task_stats(
    db: AsyncSession = Depends(get_db),
):
    queue = get_task_queue()
    stats = await queue.get_stats(db)
    return TaskQueueStats(**stats)


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AiTask).where(AiTask.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskResponse.model_validate(task)


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
):
    queue = get_task_queue()
    cancelled = await queue.cancel(db, task_id)
    if not cancelled:
        raise HTTPException(status_code=400, detail="Task cannot be cancelled")
    result = await db.execute(select(AiTask).where(AiTask.id == task_id))
    return TaskResponse.model_validate(result.scalar_one())