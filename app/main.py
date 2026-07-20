"""FastAPI application with production-safe startup, health checks, and shutdown.

Improvements in this hardening batch:
- ``/health`` now includes a database connectivity probe (critical for Render
  free-tier cold-start monitoring and load-balancer health checks).
- Lifespan startup failures (scheduler, auto-reply listeners, Telegram bot) are
  *isolated* — one component failing does not prevent the app from starting.
  Errors are logged and the app continues without the failed component.
- ``ProxyHeadersMiddleware`` ensures ``request.client.host`` / ``X-Forwarded-For``
  resolve correctly when the app runs behind nginx or Cloudflare.
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import text

from app.api.account_health import router as account_health_router
from app.api.account_health_summary import router as account_health_summary_router
from app.api.accounts import router as accounts_router
from app.api.batch import router as batch_router
from app.api.admin import router as admin_router
from app.api.ai_assist import router as ai_assist_router
from app.api.ai_copilot import router as ai_copilot_router
from app.api.ai import router as ai_router
from app.api.auth import router as auth_router
from app.api.auto_reply import router as auto_reply_router
from app.api.billing import router as billing_router
from app.api.delivery_analytics import router as delivery_analytics_router
from app.api.features import router as features_router
from app.api.free_api_key import router as free_api_key_router
from app.api.broadcast import router as broadcast_router
from app.api.campaign import router as campaign_router
from app.api.search import router as search_router
from app.api.schedule import router as schedule_router
from app.api.join_queue import router as join_queue_router
from app.api.team import router as team_router
from app.api.webhook_settings import router as webhook_settings_router
from app.api.ai_reply_v2 import router as ai_reply_v2_router
from app.api.ai_chat_v2 import router as ai_chat_v2_router
from app.api.mcp_gateway import router as mcp_gateway_router
from app.api.chat import router as chat_router
from app.api.deps import require_api_key_or_admin
from app.api.channel_hub import router as channel_hub_router
from app.api.folder import router as folder_router
from app.api.group_search import router as group_search_router
from app.api.groups import router as groups_router
from app.api.link_inspector import router as link_inspector_router
from app.api.logs import router as logs_router
from app.api.message_template import router as message_template_router
from app.api.reply_macro import router as reply_macro_router
from app.api.scheduler import router as scheduler_router
from app.api.telegram_auth import router as telegram_auth_router
from app.api.preview import router as preview_router
from app.api.telegram_verify import router as telegram_verify_router
from app.api.ai_agent import router as ai_agent_router
from app.api.content_studio import router as content_studio_router
from app.api.style_profiles import router as style_profiles_router
from app.api.tokens import router as tokens_router
from app.routers.guest_routes import router as guest_routes_router
from app.routers.stars_payments import router as stars_payments_router
from app.routers.trigger_routes import router as trigger_routes_router
from app.routers.draft_routes import router as draft_routes_router
from app.api.ai_group_intel import router as ai_group_intel_router
from app.routers.ai_admin import router as ai_employee_admin_router
from app.api.usdt_payment import router as usdt_payment_router
from app.config import settings
from app.core.logging import configure_logging, get_logger
from app.database import async_session_maker
from app.scheduler.scheduler import shutdown_scheduler, start_scheduler
from app.services.auto_reply_service import attach_all_active_listeners
from app.services.telegram_bot_service import start_bot, stop_bot
from app.services.telethon_pool import pool

# ── AI Platform Imports ───────────────────────────────────────────────
from app.ai.routers import (
    tools_router as ai_tools_router,
    workflows_router as ai_workflows_router,
    tasks_router as ai_tasks_router,
    events_router as ai_events_router,
    schedules_router as ai_schedules_router,
    plugins_router as ai_plugins_router,
    providers_router as ai_providers_router,
)
from app.ai.tools.builtin_tools import register_builtin_tools
from app.ai.tools.registry import get_tool_registry
from app.ai.task_queue.worker import get_task_worker
from app.ai.scheduler.service import get_ai_scheduler_service
from app.ai.event_bus.bus import get_event_bus
from app.ai.plugin.manager import get_plugin_manager

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: start services on boot, stop them on shutdown.

    Each startup step is wrapped in try/except so a failure in one
    (e.g. scheduler DB error, unauthenticated account, missing bot token)
    does not prevent the HTTP server from starting — the app is degraded
    but still serving health checks and API calls.
    """
    # ── Scheduler ──────────────────────────────────────────────────────
    try:
        start_scheduler()
        logger.info("scheduler_started")
    except Exception as exc:
        logger.error("scheduler_startup_failed", error=str(exc))

    # ── Auto-reply listeners ───────────────────────────────────────────
    try:
        await attach_all_active_listeners()
        logger.info("auto_reply_listeners_attached")
    except Exception as exc:
        logger.error("auto_reply_listeners_startup_failed", error=str(exc))

    # ── Telegram bot (optional) ────────────────────────────────────────
    try:
        await start_bot()
    except Exception as exc:
        logger.error("telegram_bot_startup_failed", error=str(exc))

    # ── AI Platform Startup ────────────────────────────────────────────
    try:
        register_builtin_tools()
        logger.info("ai_builtin_tools_registered")
    except Exception as exc:
        logger.error("ai_builtin_tools_registration_failed", error=str(exc))

    try:
        worker = get_task_worker()
        worker.start()
        logger.info("ai_task_worker_started")
    except Exception as exc:
        logger.error("ai_task_worker_startup_failed", error=str(exc))

    try:
        scheduler = get_ai_scheduler_service()
        scheduler.start()
        logger.info("ai_scheduler_started")
    except Exception as exc:
        logger.error("ai_scheduler_startup_failed", error=str(exc))

    try:
        manager = get_plugin_manager()
        await manager.discover_and_load()
        logger.info("ai_plugins_loaded")
    except Exception as exc:
        logger.error("ai_plugins_load_failed", error=str(exc))

    logger.info("app_started")
    yield

    # ── Shutdown ───────────────────────────────────────────────────────
    try:
        await stop_bot()
    except Exception as exc:
        logger.error("telegram_bot_shutdown_failed", error=str(exc))

    try:
        shutdown_scheduler()
    except Exception as exc:
        logger.error("scheduler_shutdown_failed", error=str(exc))

    try:
        await pool.disconnect_all()
    except Exception as exc:
        logger.error("pool_disconnect_failed", error=str(exc))

    # ── AI Platform Shutdown ───────────────────────────────────────────
    try:
        ai_scheduler = get_ai_scheduler_service()
        await ai_scheduler.stop()
    except Exception as exc:
        logger.error("ai_scheduler_shutdown_failed", error=str(exc))

    try:
        worker = get_task_worker()
        await worker.stop()
    except Exception as exc:
        logger.error("ai_task_worker_shutdown_failed", error=str(exc))

    logger.info("app_stopped")


app = FastAPI(
    title="Telegram Management Dashboard API",
    lifespan=lifespan,
    debug=settings.debug,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    openapi_url="/openapi.json" if settings.debug else None,
)

# ── Middleware stack ───────────────────────────────────────────────────

if settings.environment.strip().lower() in ("production", "prod"):
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[
            "telemon.online",
            "www.telemon.online",
            "app.telemon.online",
            "api.telemon.online",
            "localhost",
            "127.0.0.1",
        ],
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ported from TeleMon/backend/monitoring.py — request-count/latency counters and
# an optional alert webhook. Deliberately NOT calling setup_structured_logging()
# here: app.core.logging already configures structured JSON logging for this
# app, and calling both would fight over the root logger's handlers.
from app.monitoring import MetricsMiddleware, get_metrics_text

app.add_middleware(MetricsMiddleware)

# ── Routers ────────────────────────────────────────────────────────────

app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(telegram_verify_router)

_auth_required = [Depends(require_api_key_or_admin)]
app.include_router(accounts_router, dependencies=_auth_required)
app.include_router(telegram_auth_router, dependencies=_auth_required)
app.include_router(groups_router, dependencies=_auth_required)
app.include_router(folder_router, dependencies=_auth_required)
app.include_router(channel_hub_router, dependencies=_auth_required)
app.include_router(broadcast_router, dependencies=_auth_required)
app.include_router(logs_router, dependencies=_auth_required)
app.include_router(scheduler_router, dependencies=_auth_required)
app.include_router(group_search_router, dependencies=_auth_required)
app.include_router(link_inspector_router, dependencies=_auth_required)
app.include_router(auto_reply_router, dependencies=_auth_required)
app.include_router(reply_macro_router, dependencies=_auth_required)
app.include_router(billing_router, dependencies=_auth_required)
app.include_router(usdt_payment_router)
app.include_router(features_router, dependencies=_auth_required)
app.include_router(free_api_key_router)
app.include_router(account_health_router, dependencies=_auth_required)
app.include_router(account_health_summary_router, dependencies=_auth_required)
app.include_router(delivery_analytics_router, dependencies=_auth_required)
app.include_router(ai_assist_router, dependencies=_auth_required)
app.include_router(ai_copilot_router, dependencies=_auth_required)
app.include_router(ai_router, dependencies=_auth_required)
app.include_router(join_queue_router, dependencies=_auth_required)
app.include_router(message_template_router, dependencies=_auth_required)
app.include_router(campaign_router, dependencies=_auth_required)
app.include_router(schedule_router, dependencies=_auth_required)
app.include_router(preview_router, dependencies=_auth_required)
app.include_router(team_router, dependencies=_auth_required)
app.include_router(search_router, dependencies=_auth_required)
app.include_router(batch_router, dependencies=_auth_required)
app.include_router(webhook_settings_router, dependencies=_auth_required)
app.include_router(ai_reply_v2_router, dependencies=_auth_required)
app.include_router(ai_chat_v2_router, dependencies=_auth_required)
app.include_router(mcp_gateway_router, dependencies=_auth_required)
app.include_router(chat_router, dependencies=_auth_required)
app.include_router(ai_agent_router, dependencies=_auth_required)
app.include_router(content_studio_router, dependencies=_auth_required)
app.include_router(style_profiles_router, dependencies=_auth_required)
app.include_router(tokens_router, dependencies=_auth_required)
app.include_router(guest_routes_router, dependencies=_auth_required)
app.include_router(stars_payments_router, dependencies=_auth_required)
app.include_router(trigger_routes_router, dependencies=_auth_required)
app.include_router(draft_routes_router, dependencies=_auth_required)

# ── AI Platform Routers ───────────────────────────────────────────────
app.include_router(ai_tools_router, dependencies=_auth_required)
app.include_router(ai_workflows_router, dependencies=_auth_required)
app.include_router(ai_group_intel_router, dependencies=_auth_required)
app.include_router(ai_employee_admin_router, dependencies=_auth_required)
app.include_router(ai_tasks_router, dependencies=_auth_required)
app.include_router(ai_events_router, dependencies=_auth_required)
app.include_router(ai_schedules_router, dependencies=_auth_required)
app.include_router(ai_plugins_router, dependencies=_auth_required)
app.include_router(ai_providers_router, dependencies=_auth_required)


@app.get("/metrics")
async def metrics():
    """Prometheus exposition-format metrics (ported from TeleMon/backend/monitoring.py)."""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(get_metrics_text())


@app.get("/health")
async def health():
    """Health check endpoint with database connectivity probe."""
    try:
        async with async_session_maker() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok", "environment": settings.environment}
    except Exception as exc:
        logger.warning("health_check_db_failed", error=str(exc))
        from fastapi.responses import JSONResponse
        from starlette.status import HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "degraded", "environment": settings.environment, "detail": "database unreachable"},
        )