from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.accounts import router as accounts_router
from app.api.admin import router as admin_router
from app.api.auto_reply import router as auto_reply_router
from app.api.broadcast import router as broadcast_router
from app.api.deps import require_api_key_or_admin
from app.api.groups import router as groups_router
from app.api.logs import router as logs_router
from app.api.scheduler import router as scheduler_router
from app.api.telegram_auth import router as telegram_auth_router
from app.config import settings
from app.core.logging import configure_logging, get_logger
from app.scheduler.scheduler import shutdown_scheduler, start_scheduler
from app.services.auto_reply_service import attach_all_active_listeners
from app.services.telegram_bot_service import start_bot, stop_bot
from app.services.telethon_pool import pool

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    await attach_all_active_listeners()
    await start_bot()
    logger.info("app_started")
    yield
    await stop_bot()
    shutdown_scheduler()
    await pool.disconnect_all()
    logger.info("app_stopped")


app = FastAPI(
    title="Telegram Management Dashboard API",
    lifespan=lifespan,
    debug=settings.debug,
    # Hide interactive API docs when not in debug mode — this app handles encrypted
    # Telegram sessions, so the schema (and "try it out" button) shouldn't be public
    # by default in a real deployment.
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    openapi_url="/openapi.json" if settings.debug else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin_router)

_auth_required = [Depends(require_api_key_or_admin)]
app.include_router(accounts_router, dependencies=_auth_required)
app.include_router(telegram_auth_router, dependencies=_auth_required)
app.include_router(groups_router, dependencies=_auth_required)
app.include_router(broadcast_router, dependencies=_auth_required)
app.include_router(logs_router, dependencies=_auth_required)
app.include_router(scheduler_router, dependencies=_auth_required)
app.include_router(auto_reply_router, dependencies=_auth_required)


@app.get("/health")
async def health():
    return {"status": "ok", "environment": settings.environment}
