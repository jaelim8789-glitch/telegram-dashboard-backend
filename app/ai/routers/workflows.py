"""AI Workflows API — manage workflow definitions and executions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.schemas.ai_workflow import (
    WorkflowDefinitionCreate,
    WorkflowDefinitionUpdate,
    WorkflowDefinitionResponse,
    WorkflowExecutionRequest,
    WorkflowExecutionResponse,
    WorkflowStepResponse,
    WorkflowListResponse,
)
from app.ai.workflow.engine import get_workflow_engine
from app.ai.workflow.executor import get_workflow_executor
from app.api.deps import get_current_tenant_id
from app.database import get_db

router = APIRouter(prefix="/ai/workflows", tags=["AI Workflows"])


@router.get("", response_model=WorkflowListResponse)
async def list_workflows(
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    engine = get_workflow_engine()
    workflows = await engine.list_definitions(db, tenant_id)
    return WorkflowListResponse(
        workflows=[WorkflowDefinitionResponse.model_validate(w) for w in workflows],
        total=len(workflows),
    )


@router.post("", response_model=WorkflowDefinitionResponse, status_code=201)
async def create_workflow(
    data: WorkflowDefinitionCreate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    engine = get_workflow_engine()
    workflow = await engine.create_definition(db, tenant_id, data.model_dump())
    return WorkflowDefinitionResponse.model_validate(workflow)


@router.get("/{workflow_id}", response_model=WorkflowDefinitionResponse)
async def get_workflow(
    workflow_id: str,
    db: AsyncSession = Depends(get_db),
):
    engine = get_workflow_engine()
    workflow = await engine.get_definition(db, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return WorkflowDefinitionResponse.model_validate(workflow)


@router.put("/{workflow_id}", response_model=WorkflowDefinitionResponse)
async def update_workflow(
    workflow_id: str,
    data: WorkflowDefinitionUpdate,
    db: AsyncSession = Depends(get_db),
):
    engine = get_workflow_engine()
    workflow = await engine.update_definition(db, workflow_id, data.model_dump(exclude_none=True))
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return WorkflowDefinitionResponse.model_validate(workflow)


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: str,
    db: AsyncSession = Depends(get_db),
):
    engine = get_workflow_engine()
    deleted = await engine.delete_definition(db, workflow_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Workflow not found")


@router.post("/execute", response_model=WorkflowExecutionResponse)
async def execute_workflow(
    request: WorkflowExecutionRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    engine = get_workflow_engine()
    execution = await engine.start_execution(db, tenant_id, request.workflow_id, request.input_data)
    if not execution:
        raise HTTPException(status_code=404, detail="Workflow not found or inactive")

    # Execute in background via task queue
    from app.ai.task_queue.queue import get_task_queue
    task_queue = get_task_queue()
    await task_queue.enqueue(
        db, tenant_id, "workflow_execution",
        {"execution_id": execution.id, "workflow_id": request.workflow_id},
        priority=request.priority,
        workflow_execution_id=execution.id,
    )

    return WorkflowExecutionResponse.model_validate(execution)


@router.get("/executions/{execution_id}", response_model=WorkflowExecutionResponse)
async def get_execution(
    execution_id: str,
    db: AsyncSession = Depends(get_db),
):
    engine = get_workflow_engine()
    execution = await engine.get_execution(db, execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    return WorkflowExecutionResponse.model_validate(execution)


@router.get("/executions/{execution_id}/steps", response_model=list[WorkflowStepResponse])
async def get_execution_steps(
    execution_id: str,
    db: AsyncSession = Depends(get_db),
):
    engine = get_workflow_engine()
    steps = await engine.get_steps(db, execution_id)
    return [WorkflowStepResponse.model_validate(s) for s in steps]