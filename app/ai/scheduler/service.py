"""
AI Scheduler Service — manages scheduled AI job definitions and triggers execution.

This is separate from the existing APScheduler-based scheduler in
app/scheduler/scheduler.py. It uses the AI platform's own schedule definitions
stored in the database and dispatches jobs through the task queue and event bus.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.config import get_ai_config
from app.ai.models.ai_schedule import AiScheduleDefinition, AiScheduleExecution
from app.ai.task_queue.queue import get_task_queue
from app.ai.event_bus.bus import get_event_bus
from app.core.logging import get_logger
from app.database import async_session_maker

logger = get_logger(__name__)


class AiSchedulerService:
    """Manages AI schedule definitions and triggers scheduled executions."""

    def __init__(self) -> None:
        self._config = get_ai_config()
        self._task_queue = get_task_queue()
        self._event_bus = get_event_bus()
        self._running = False
        self._tick_task: asyncio.Task[None] | None = None

    # ── Schedule Definition Management ───────────────────────────────

    async def create_schedule(
        self, db: AsyncSession, tenant_id: str, data: dict[str, Any]
    ) -> AiScheduleDefinition:
        """Create a new schedule definition."""
        schedule = AiScheduleDefinition(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            **{k: v for k, v in data.items() if hasattr(AiScheduleDefinition, k)},
        )
        db.add(schedule)
        await db.commit()
        await db.refresh(schedule)
        logger.info("schedule_created", name=schedule.name, tenant_id=tenant_id)
        return schedule

    async def update_schedule(
        self, db: AsyncSession, schedule_id: str, data: dict[str, Any]
    ) -> AiScheduleDefinition | None:
        """Update a schedule definition."""
        result = await db.execute(
            select(AiScheduleDefinition).where(AiScheduleDefinition.id == schedule_id)
        )
        schedule = result.scalar_one_or_none()
        if not schedule:
            return None
        for key, value in data.items():
            if hasattr(schedule, key) and value is not None:
                setattr(schedule, key, value)
        await db.commit()
        await db.refresh(schedule)
        logger.info("schedule_updated", name=schedule.name)
        return schedule

    async def delete_schedule(self, db: AsyncSession, schedule_id: str) -> bool:
        """Delete a schedule definition."""
        result = await db.execute(
            select(AiScheduleDefinition).where(AiScheduleDefinition.id == schedule_id)
        )
        schedule = result.scalar_one_or_none()
        if not schedule:
            return False
        await db.delete(schedule)
        await db.commit()
        logger.info("schedule_deleted", name=schedule.name)
        return True

    async def list_schedules(
        self, db: AsyncSession, tenant_id: str, active_only: bool = True
    ) -> list[AiScheduleDefinition]:
        """List schedules for a tenant."""
        query = select(AiScheduleDefinition).where(
            AiScheduleDefinition.tenant_id == tenant_id
        )
        if active_only:
            query = query.where(AiScheduleDefinition.is_active == True)
        query = query.order_by(AiScheduleDefinition.created_at.desc())
        result = await db.execute(query)
        return list(result.scalars().all())

    # ── Execution ────────────────────────────────────────────────────

    async def execute_schedule(
        self, db: AsyncSession, schedule: AiScheduleDefinition
    ) -> AiScheduleExecution | None:
        """Execute a schedule's action and record the execution."""
        # Create execution record
        execution = AiScheduleExecution(
            id=str(uuid.uuid4()),
            schedule_id=schedule.id,
            tenant_id=schedule.tenant_id,
            status="running",
            triggered_at=datetime.now(timezone.utc),
        )
        db.add(execution)
        await db.commit()

        try:
            action_type = schedule.action_type
            action_config = schedule.action_config

            if action_type == "tool_call":
                # Enqueue as a task
                await self._task_queue.enqueue(
                    db,
                    schedule.tenant_id,
                    "tool_execution",
                    {
                        "tool_name": action_config.get("tool_name"),
                        "arguments": action_config.get("arguments", {}),
                    },
                )
                result = {"status": "enqueued"}

            elif action_type == "workflow_run":
                await self._task_queue.enqueue(
                    db,
                    schedule.tenant_id,
                    "workflow_execution",
                    {
                        "workflow_id": action_config.get("workflow_id"),
                        "input_data": action_config.get("input", {}),
                    },
                )
                result = {"status": "enqueued"}

            elif action_type == "event_emit":
                await self._event_bus.publish(
                    action_config.get("event_type", "schedule.triggered"),
                    action_config.get("payload", {}),
                    source="scheduler",
                    db=db,
                    tenant_id=schedule.tenant_id,
                )
                result = {"status": "emitted"}

            elif action_type == "task_enqueue":
                await self._task_queue.enqueue(
                    db,
                    schedule.tenant_id,
                    action_config.get("task_type", "custom"),
                    action_config.get("payload", {}),
                )
                result = {"status": "enqueued"}

            else:
                result = {"status": "unsupported_action_type"}

            execution.status = "completed"
            execution.result = result
            execution.completed_at = datetime.now(timezone.utc)
            await db.commit()

            # Increment execution counter
            schedule.current_executions += 1

            # Check if max executions reached
            if schedule.max_executions > 0 and schedule.current_executions >= schedule.max_executions:
                schedule.is_active = False
                logger.info("schedule_max_executions_reached", name=schedule.name)

            await db.commit()
            await db.refresh(execution)
            logger.info("schedule_executed", name=schedule.name, action=action_type)
            return execution

        except Exception as exc:
            execution.status = "failed"
            execution.error_message = str(exc)
            execution.completed_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(execution)
            logger.error("schedule_execution_failed", name=schedule.name, error=str(exc))
            return execution

    # ── Tick Loop ────────────────────────────────────────────────────

    async def tick(self) -> int:
        """Check all active schedules and execute due ones.

        Returns the number of schedules triggered.
        """
        async with async_session_maker() as db:
            result = await db.execute(
                select(AiScheduleDefinition).where(
                    AiScheduleDefinition.is_active == True,
                )
            )
            schedules = result.scalars().all()

        triggered = 0
        now = datetime.now(timezone.utc)

        for schedule in schedules:
            try:
                should_run = False

                if schedule.schedule_type == "once":
                    if schedule.run_at and schedule.run_at.replace(tzinfo=timezone.utc) <= now:
                        if schedule.current_executions == 0:
                            should_run = True

                elif schedule.schedule_type == "interval":
                    if schedule.interval_seconds:
                        # Check if enough time has passed since last execution
                        async with async_session_maker() as db:
                            last_exec = await db.execute(
                                select(AiScheduleExecution)
                                .where(AiScheduleExecution.schedule_id == schedule.id)
                                .order_by(AiScheduleExecution.triggered_at.desc())
                                .limit(1)
                            )
                            last = last_exec.scalar_one_or_none()
                            if not last:
                                should_run = True
                            else:
                                elapsed = (now - last.triggered_at.replace(tzinfo=timezone.utc)).total_seconds()
                                if elapsed >= schedule.interval_seconds:
                                    should_run = True

                elif schedule.schedule_type == "cron":
                    # Simplified cron check - in production use a proper cron parser
                    should_run = True  # Placeholder

                if should_run:
                    async with async_session_maker() as db:
                        await self.execute_schedule(db, schedule)
                    triggered += 1

            except Exception as exc:
                logger.error("schedule_tick_error", schedule_id=schedule.id, error=str(exc))

        return triggered

    def start(self) -> None:
        """Start the scheduler tick loop."""
        if self._running:
            return
        self._running = True
        self._tick_task = asyncio.create_task(self._tick_loop())
        logger.info("ai_scheduler_started", tick_seconds=self._config.scheduler_tick_seconds)

    async def stop(self) -> None:
        """Stop the scheduler tick loop."""
        self._running = False
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
            self._tick_task = None
        logger.info("ai_scheduler_stopped")

    async def _tick_loop(self) -> None:
        """Main scheduler loop."""
        while self._running:
            try:
                triggered = await self.tick()
                if triggered:
                    logger.info("scheduler_tick", triggered=triggered)
            except Exception as exc:
                logger.error("scheduler_tick_error", error=str(exc))
            await asyncio.sleep(self._config.scheduler_tick_seconds)


# ── Singleton ─────────────────────────────────────────────────────────

_service: AiSchedulerService | None = None


def get_ai_scheduler_service() -> AiSchedulerService:
    """Get the singleton AI scheduler service instance."""
    global _service
    if _service is None:
        _service = AiSchedulerService()
    return _service