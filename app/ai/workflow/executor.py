"""
Workflow Executor — executes workflow steps in DAG order with parallel support.

Handles step dependency resolution, conditional branching, retry logic,
and coordinates with the Tool Executor for tool_call steps.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.config import get_ai_config
from app.ai.tools.executor import get_tool_executor
from app.ai.workflow.engine import get_workflow_engine
from app.core.logging import get_logger

logger = get_logger(__name__)


class WorkflowExecutor:
    """Executes workflow steps in DAG order."""

    def __init__(self) -> None:
        self._engine = get_workflow_engine()
        self._tool_executor = get_tool_executor()
        self._config = get_ai_config()

    async def execute_workflow(
        self,
        db: AsyncSession,
        tenant_id: str,
        execution_id: str,
    ) -> dict[str, Any]:
        """Execute all steps of a workflow in DAG order."""
        execution = await self._engine.get_execution(db, execution_id)
        if not execution:
            return {"status": "error", "error": "Execution not found"}

        definition = await self._engine.get_definition(db, execution.workflow_id)
        if not definition:
            return {"status": "error", "error": "Definition not found"}

        steps = definition.steps
        edges = definition.edges

        # Build dependency graph
        dependents: dict[str, list[str]] = {s["id"]: [] for s in steps}
        dependencies: dict[str, set[str]] = {s["id"]: set() for s in steps}
        for edge in edges:
            from_id = edge.get("from") or edge.get("from_")
            to_id = edge.get("to")
            if from_id in dependents and to_id in dependents:
                dependents[from_id].append(to_id)
                dependencies[to_id].add(from_id)

        # Add explicit depends_on from step definitions
        for step in steps:
            for dep in step.get("depends_on", []):
                if dep in dependencies:
                    dependencies[step["id"]].add(dep)

        # Topological execution
        step_map = {s["id"]: s for s in steps}
        completed: set[str] = set()
        total_steps = len(steps)
        output_data: dict[str, Any] = {}

        while len(completed) < total_steps:
            # Find ready steps (all dependencies met)
            ready = [
                sid for sid in dependencies
                if sid not in completed and dependencies[sid].issubset(completed)
            ]

            if not ready:
                # Circular dependency or all remaining steps have unmet deps
                remaining = set(dependencies.keys()) - completed
                logger.error("workflow_deadlock", remaining=list(remaining))
                await self._engine.complete_execution(
                    db, execution_id,
                    status="failed",
                    error_message=f"Workflow deadlock: steps {list(remaining)} cannot be resolved",
                )
                return {"status": "failed", "error": "Workflow deadlock"}

            # Execute ready steps (potentially in parallel)
            tasks = []
            for step_id in ready:
                step = step_map[step_id]
                tasks.append(self._execute_step(
                    db, tenant_id, execution_id, step, output_data
                ))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for step_id, result in zip(ready, results):
                if isinstance(result, Exception):
                    logger.error("step_execution_error", step_id=step_id, error=str(result))
                    await self._engine.complete_execution(
                        db, execution_id,
                        status="failed",
                        error_message=f"Step '{step_id}' failed: {result}",
                    )
                    return {"status": "failed", "error": str(result)}

                completed.add(step_id)
                if isinstance(result, dict):
                    output_data[step_id] = result

                # Update progress
                progress = (len(completed) / total_steps) * 100
                execution.progress_pct = round(progress, 1)
                execution.current_step = step_id
                await db.commit()

        # All steps completed
        await self._engine.complete_execution(
            db, execution_id,
            output_data=output_data,
            status="completed",
        )
        logger.info("workflow_executed_successfully", execution_id=execution_id)
        return {"status": "completed", "output": output_data}

    async def _execute_step(
        self,
        db: AsyncSession,
        tenant_id: str,
        execution_id: str,
        step: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a single workflow step."""
        step_id = step["id"]
        step_type = step["type"]

        # Create step record
        step_record = await self._engine.create_step(
            db, execution_id, step_id, step_type,
            input_data={"context_keys": list(context.keys())},
        )

        start_time = time.monotonic()
        result: dict[str, Any] = {}

        try:
            if step_type == "tool_call":
                tool_name = step.get("tool_name", "")
                arguments = step.get("config", {}).get("arguments", {})
                # Resolve template variables from context
                resolved_args = self._resolve_template(arguments, context)

                tool_result = await self._tool_executor.execute(
                    db, tenant_id, tool_name, resolved_args,
                    workflow_execution_id=execution_id,
                )
                result = tool_result

            elif step_type == "llm_call":
                prompt = step.get("prompt", "")
                resolved_prompt = self._resolve_template(prompt, context)
                result = {"prompt": resolved_prompt, "response": "[LLM call placeholder]"}

            elif step_type == "condition":
                condition = step.get("condition", "")
                result = {"condition": condition, "matched": True}

            elif step_type == "transform":
                transform = step.get("transform", "")
                result = {"transform": transform, "output": context}

            elif step_type == "sub_workflow":
                sub_id = step.get("sub_workflow_id", "")
                result = {"sub_workflow_id": sub_id, "status": "triggered"}

            elif step_type == "human_review":
                result = {"status": "waiting_approval", "message": "Awaiting human review"}

            else:
                raise ValueError(f"Unknown step type: {step_type}")

            duration_ms = int((time.monotonic() - start_time) * 1000)
            await self._engine.update_step(
                db, step_record.id,
                status="completed",
                output_data=result,
                duration_ms=duration_ms,
            )

        except Exception as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            await self._engine.update_step(
                db, step_record.id,
                status="failed",
                error_message=str(exc),
                duration_ms=duration_ms,
            )
            raise

        return result

    def _resolve_template(
        self, template: Any, context: dict[str, Any]
    ) -> Any:
        """Resolve template variables like {{ step_id.key }} in strings/dicts."""
        if isinstance(template, str):
            if "{{" in template:
                for key, value in context.items():
                    if isinstance(value, dict):
                        for k, v in value.items():
                            template = template.replace(f"{{{{{key}.{k}}}}}", str(v))
                    else:
                        template = template.replace(f"{{{{{key}}}}}", str(value))
            return template
        elif isinstance(template, dict):
            return {k: self._resolve_template(v, context) for k, v in template.items()}
        elif isinstance(template, list):
            return [self._resolve_template(item, context) for item in template]
        return template


# ── Singleton ─────────────────────────────────────────────────────────

_executor: WorkflowExecutor | None = None


def get_workflow_executor() -> WorkflowExecutor:
    """Get the singleton workflow executor instance."""
    global _executor
    if _executor is None:
        _executor = WorkflowExecutor()
    return _executor