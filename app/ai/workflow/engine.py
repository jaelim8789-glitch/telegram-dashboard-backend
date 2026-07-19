"""
Workflow Engine — manages workflow definitions and execution lifecycle.

Supports DAG-based workflows with conditional branching, parallel execution,
retry logic, and human-in-the-loop approval steps.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.config import get_ai_config
from app.ai.models.ai_workflow import AiWorkflowDefinition, AiWorkflowExecution, AiWorkflowStep
from app.core.logging import get_logger

logger = get_logger(__name__)


class WorkflowEngine:
    """Manages workflow definitions and execution lifecycle."""

    def __init__(self) -> None:
        self._config = get_ai_config()

    # ── Definition Management ─────────────────────────────────────────

    async def create_definition(
        self, db: AsyncSession, tenant_id: str, data: dict[str, Any]
    ) -> AiWorkflowDefinition:
        """Create a new workflow definition."""
        workflow = AiWorkflowDefinition(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            **{k: v for k, v in data.items() if hasattr(AiWorkflowDefinition, k)},
        )
        db.add(workflow)
        await db.commit()
        await db.refresh(workflow)
        logger.info("workflow_definition_created", name=workflow.name, tenant_id=tenant_id)
        return workflow

    async def update_definition(
        self, db: AsyncSession, workflow_id: str, data: dict[str, Any]
    ) -> AiWorkflowDefinition | None:
        """Update an existing workflow definition."""
        result = await db.execute(
            select(AiWorkflowDefinition).where(AiWorkflowDefinition.id == workflow_id)
        )
        workflow = result.scalar_one_or_none()
        if not workflow:
            return None
        for key, value in data.items():
            if hasattr(workflow, key) and value is not None:
                setattr(workflow, key, value)
        workflow.version += 1
        await db.commit()
        await db.refresh(workflow)
        logger.info("workflow_definition_updated", name=workflow.name)
        return workflow

    async def delete_definition(self, db: AsyncSession, workflow_id: str) -> bool:
        """Delete a workflow definition."""
        result = await db.execute(
            select(AiWorkflowDefinition).where(AiWorkflowDefinition.id == workflow_id)
        )
        workflow = result.scalar_one_or_none()
        if not workflow:
            return False
        await db.delete(workflow)
        await db.commit()
        logger.info("workflow_definition_deleted", name=workflow.name)
        return True

    async def get_definition(
        self, db: AsyncSession, workflow_id: str
    ) -> AiWorkflowDefinition | None:
        """Get a workflow definition by ID."""
        result = await db.execute(
            select(AiWorkflowDefinition).where(AiWorkflowDefinition.id == workflow_id)
        )
        return result.scalar_one_or_none()

    async def list_definitions(
        self, db: AsyncSession, tenant_id: str, active_only: bool = True
    ) -> list[AiWorkflowDefinition]:
        """List workflow definitions for a tenant."""
        query = select(AiWorkflowDefinition).where(
            AiWorkflowDefinition.tenant_id == tenant_id
        )
        if active_only:
            query = query.where(AiWorkflowDefinition.is_active == True)
        query = query.order_by(AiWorkflowDefinition.created_at.desc())
        result = await db.execute(query)
        return list(result.scalars().all())

    # ── Execution Lifecycle ───────────────────────────────────────────

    async def start_execution(
        self,
        db: AsyncSession,
        tenant_id: str,
        workflow_id: str,
        input_data: dict[str, Any] | None = None,
    ) -> AiWorkflowExecution | None:
        """Start a new workflow execution."""
        definition = await self.get_definition(db, workflow_id)
        if not definition or not definition.is_active:
            logger.warning("workflow_not_found_or_inactive", workflow_id=workflow_id)
            return None

        execution = AiWorkflowExecution(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            workflow_name=definition.name,
            status="running",
            input_data=input_data or {},
            started_at=datetime.now(timezone.utc),
        )
        db.add(execution)
        await db.commit()
        await db.refresh(execution)

        logger.info(
            "workflow_execution_started",
            execution_id=execution.id,
            workflow_name=definition.name,
        )
        return execution

    async def complete_execution(
        self,
        db: AsyncSession,
        execution_id: str,
        output_data: dict[str, Any] | None = None,
        status: str = "completed",
        error_message: str | None = None,
    ) -> AiWorkflowExecution | None:
        """Mark a workflow execution as completed/failed."""
        result = await db.execute(
            select(AiWorkflowExecution).where(AiWorkflowExecution.id == execution_id)
        )
        execution = result.scalar_one_or_none()
        if not execution:
            return None

        execution.status = status
        execution.output_data = output_data
        execution.error_message = error_message
        execution.progress_pct = 100.0 if status == "completed" else execution.progress_pct
        execution.completed_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(execution)

        logger.info("workflow_execution_completed", execution_id=execution_id, status=status)
        return execution

    async def get_execution(
        self, db: AsyncSession, execution_id: str
    ) -> AiWorkflowExecution | None:
        """Get a workflow execution by ID."""
        result = await db.execute(
            select(AiWorkflowExecution).where(AiWorkflowExecution.id == execution_id)
        )
        return result.scalar_one_or_none()

    async def list_executions(
        self,
        db: AsyncSession,
        tenant_id: str,
        status: str | None = None,
        limit: int = 50,
    ) -> list[AiWorkflowExecution]:
        """List workflow executions for a tenant."""
        query = select(AiWorkflowExecution).where(
            AiWorkflowExecution.tenant_id == tenant_id
        )
        if status:
            query = query.where(AiWorkflowExecution.status == status)
        query = query.order_by(AiWorkflowExecution.created_at.desc()).limit(limit)
        result = await db.execute(query)
        return list(result.scalars().all())

    # ── Step Management ───────────────────────────────────────────────

    async def create_step(
        self,
        db: AsyncSession,
        execution_id: str,
        step_id: str,
        step_type: str,
        input_data: dict[str, Any] | None = None,
    ) -> AiWorkflowStep:
        """Create a step record for a workflow execution."""
        step = AiWorkflowStep(
            id=str(uuid.uuid4()),
            execution_id=execution_id,
            step_id=step_id,
            step_type=step_type,
            status="pending",
            input_data=input_data or {},
        )
        db.add(step)
        await db.commit()
        await db.refresh(step)
        return step

    async def update_step(
        self,
        db: AsyncSession,
        step_id: str,
        status: str,
        output_data: dict[str, Any] | None = None,
        error_message: str | None = None,
        duration_ms: int | None = None,
    ) -> AiWorkflowStep | None:
        """Update a step's status and output."""
        result = await db.execute(
            select(AiWorkflowStep).where(AiWorkflowStep.id == step_id)
        )
        step = result.scalar_one_or_none()
        if not step:
            return None

        step.status = status
        if output_data is not None:
            step.output_data = output_data
        if error_message is not None:
            step.error_message = error_message
        if duration_ms is not None:
            step.duration_ms = duration_ms
        if status in ("completed", "failed", "skipped"):
            step.completed_at = datetime.now(timezone.utc)
        if status == "running" and step.started_at is None:
            step.started_at = datetime.now(timezone.utc)

        await db.commit()
        await db.refresh(step)
        return step

    async def get_steps(
        self, db: AsyncSession, execution_id: str
    ) -> list[AiWorkflowStep]:
        """Get all steps for a workflow execution."""
        result = await db.execute(
            select(AiWorkflowStep)
            .where(AiWorkflowStep.execution_id == execution_id)
            .order_by(AiWorkflowStep.created_at)
        )
        return list(result.scalars().all())


# ── Singleton ─────────────────────────────────────────────────────────

_engine: WorkflowEngine | None = None


def get_workflow_engine() -> WorkflowEngine:
    """Get the singleton workflow engine instance."""
    global _engine
    if _engine is None:
        _engine = WorkflowEngine()
    return _engine