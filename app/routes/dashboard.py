from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

dashboard_router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


class BatchRequest(BaseModel):
    widgets: list[str]


@dashboard_router.post("/batch")
async def dashboard_batch(req: BatchRequest):
    result = {}

    try:
        from app.api.telemon_memory import router as telemon_memory_router
    except ImportError:
        pass

    for widget in req.widgets:
        try:
            if widget == "overview":
                from app.services.dashboard import get_overview
                result["overview"] = await get_overview()
            elif widget == "health":
                from app.services.dashboard import get_health
                result["health"] = await get_health()
            elif widget == "logs":
                from app.services.dashboard import get_recent_logs
                result["logs"] = await get_recent_logs(limit=10)
            elif widget == "scheduler":
                from app.services.dashboard import get_scheduler_status
                result["scheduler"] = await get_scheduler_status()
            elif widget == "telememory":
                from app.services.dashboard import get_telememory_snapshot
                result["telememory"] = await get_telememory_snapshot()
            else:
                result[widget] = None
        except Exception as e:
            result[widget] = {"error": str(e)}

    return result
