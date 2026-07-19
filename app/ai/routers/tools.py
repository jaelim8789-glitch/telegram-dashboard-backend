"""AI Tools API — manage tool definitions and execute tool calls."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.schemas.ai_tool import (
    ToolDefinitionCreate,
    ToolDefinitionUpdate,
    ToolDefinitionResponse,
    ToolExecutionRequest,
    ToolExecutionResponse,
    ToolExecutionLogResponse,
    ToolListResponse,
)
from app.ai.tools.registry import get_tool_registry
from app.ai.tools.executor import get_tool_executor
from app.api.deps import get_current_tenant_id
from app.database import get_db

router = APIRouter(prefix="/ai/tools", tags=["AI Tools"])


@router.get("", response_model=ToolListResponse)
async def list_tools(
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    registry = get_tool_registry()
    if not registry.is_loaded:
        await registry.load_from_db(db)
    tools = registry.list_definitions()
    return ToolListResponse(
        tools=[ToolDefinitionResponse.model_validate(t) for t in tools],
        total=len(tools),
    )


@router.post("", response_model=ToolDefinitionResponse, status_code=201)
async def create_tool(
    data: ToolDefinitionCreate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    registry = get_tool_registry()
    tool = await registry.create_definition(db, tenant_id, data.model_dump())
    return ToolDefinitionResponse.model_validate(tool)


@router.get("/{tool_id}", response_model=ToolDefinitionResponse)
async def get_tool(
    tool_id: str,
    db: AsyncSession = Depends(get_db),
):
    registry = get_tool_registry()
    if not registry.is_loaded:
        await registry.load_from_db(db)
    for tool in registry.list_definitions():
        if tool.id == tool_id:
            return ToolDefinitionResponse.model_validate(tool)
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="Tool not found")


@router.put("/{tool_id}", response_model=ToolDefinitionResponse)
async def update_tool(
    tool_id: str,
    data: ToolDefinitionUpdate,
    db: AsyncSession = Depends(get_db),
):
    registry = get_tool_registry()
    tool = await registry.update_definition(db, tool_id, data.model_dump(exclude_none=True))
    if not tool:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Tool not found")
    return ToolDefinitionResponse.model_validate(tool)


@router.delete("/{tool_id}", status_code=204)
async def delete_tool(
    tool_id: str,
    db: AsyncSession = Depends(get_db),
):
    registry = get_tool_registry()
    deleted = await registry.delete_definition(db, tool_id)
    if not deleted:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Tool not found")


@router.post("/execute", response_model=ToolExecutionResponse)
async def execute_tool(
    request: ToolExecutionRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    executor = get_tool_executor()
    registry = get_tool_registry()
    if not registry.is_loaded:
        await registry.load_from_db(db)

    result = await executor.execute(
        db, tenant_id, request.tool_name, request.arguments,
        session_id=request.session_id,
        workflow_execution_id=request.workflow_execution_id,
        task_id=request.task_id,
        timeout_seconds=request.timeout_seconds,
    )
    return ToolExecutionResponse(**result)


@router.get("/schemas/openai")
async def get_openai_tool_schemas(
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    registry = get_tool_registry()
    if not registry.is_loaded:
        await registry.load_from_db(db)
    return {"tools": registry.get_openai_tools_schema()}