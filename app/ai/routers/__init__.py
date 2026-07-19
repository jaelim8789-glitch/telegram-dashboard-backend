"""AI Platform FastAPI routers."""

from app.ai.routers.tools import router as tools_router
from app.ai.routers.workflows import router as workflows_router
from app.ai.routers.tasks import router as tasks_router
from app.ai.routers.events import router as events_router
from app.ai.routers.schedules import router as schedules_router
from app.ai.routers.plugins import router as plugins_router
from app.ai.routers.providers import router as providers_router

__all__ = [
    "tools_router",
    "workflows_router",
    "tasks_router",
    "events_router",
    "schedules_router",
    "plugins_router",
    "providers_router",
]