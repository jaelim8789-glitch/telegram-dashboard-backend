# Legacy `backend/` → `telegram-dashboard-backend/app/` Migration Checklist

## Status of `backend/app/__init__.py`

**Does not exist** at `backend/app/__init__.py`. The `backend/` directory is a standalone FastAPI app (SQLite-based, Telethon account runtime) and has no `app/` sub-package. `telegram-dashboard-backend` uses `app/` as its top-level package (SQLAlchemy + FastAPI). The bot service in `telegram-dashboard-backend` imports `from app import ...` — this refers to its own `telegram-dashboard-backend/app/`, not `backend/app/`.

## Key `backend/` files vs. `telegram-dashboard-backend/` counterparts

| File | Purpose | Has counterpart in `app/api/`? | Has counterpart in `app/routers/`? |
|---|---|---|---|
| `backend/main.py` | FastAPI app factory, lifespan, DB init, WAL mode, backup loop, router registration | ❌ (functionality is in `telegram-dashboard-backend/app/main.py` and `app/database.py`) | N/A |
| `backend/admin_platform.py` | RBAC, plan engine, feature flags, API key manager, trial/billing, audit logs (SQLite-based) | ❌ (admin functionality is spread across `app/api/admin.py`, `app/core/security.py`, `app/crud/`) | N/A |
| `backend/auth_middleware.py` | Unified auth session + API key validation middleware for legacy backend | ❌ (auth is in `app/api/auth.py`, `app/api/deps.py`) | N/A |
| `backend/routers/accounts.py` | Account CRUD + Telethon session management | ✅ `app/api/accounts.py` | ❌ |
| `backend/routers/auth.py` | Authentication (session + API key) | ✅ `app/api/auth.py` | ❌ |
| `backend/routers/broadcast.py` | Broadcast messaging | ✅ `app/api/broadcast.py` | ❌ |
| `backend/routers/auto_reply.py` | Auto-reply rules | ✅ `app/api/auto_reply.py` | ❌ |
| `backend/routers/reply_macro.py` | Scheduled reply macros | ✅ `app/api/reply_macro.py` | ❌ |
| `backend/routers/health.py` | Health check | ❌ (no dedicated health endpoint) | ❌ |
| `backend/routers/groups.py` | Group/channel listing | ✅ `app/api/groups.py` | ❌ |
| `backend/routers/runtime_inspector.py` | Runtime debug inspector | ❌ (some in `app/api/runtime.py`) | ❌ |
| `backend/routers/folders.py` | Telegram folder management | ✅ `app/api/folder.py`, `app/api/smart_folders.py` | ❌ |
| `backend/routers/healing.py` | Account healing/self-repair | ✅ `app/api/account_health.py`, `app/api/account_self_reset.py` | ❌ |
| `backend/routers/admin.py` | Admin dashboard, user management | ✅ `app/api/admin.py` | ❌ |
| `backend/routers/free_api_key.py` | Free tier API key | ✅ `app/api/free_api_key.py` | ❌ |
| `backend/routers/guest_routes.py` | Guest/unauthenticated access | ❌ | ✅ `app/routers/guest_routes.py` |
| `backend/routers/stars_payments.py` | Telegram Stars payments | ❌ | ✅ `app/routers/stars_payments.py` |
| `backend/routers/draft_routes.py` | Draft message management | ❌ | ✅ `app/routers/draft_routes.py` |
| `backend/routers/trigger_routes.py` | Trigger-based actions | ❌ | ✅ `app/routers/trigger_routes.py` |
| `backend/routers/ai_admin.py` | AI admin configuration | ❌ (AI features in `app/api/ai*.py`) | ✅ `app/routers/ai_admin.py` |
| `backend/routers/cryptomus_payments.py` | Cryptomus payment integration | ❌ | ❌ (Cryptomus is in `app/api/nowpayments.py` style + `backend/cryptomus.py`) |
| `backend/routers/telegram_bot.py` | Telegram bot webhook routes (legacy) | ❌ | ❌ (handled by PTB bot service) |

## Other files in `backend/`

| File | Purpose | Has counterpart? |
|---|---|---|
| `backend/account_runtime.py` | Per-account Telethon runtime | ✅ Partially in `telegram-dashboard-backend/app/services/telegram_*.py` and account-health services |
| `backend/runtime_manager.py` | Manages all account runtimes | ❌ (runtime management is handled differently in the new stack) |
| `backend/event_bus.py` | In-process event pub/sub | ❌ (not needed — new stack uses direct service calls) |
| `backend/models.py` | Legacy SQLite ORM models | ✅ Replaced by SQLAlchemy models in `app/models/` |
| `backend/media.py` | Media upload/download handling | ❌ (may be in `app/services/` or frontend) |
| `backend/monitoring.py` | Prometheus metrics + alerts | ❌ (not migrated) |
| `backend/opentelemetry_setup.py` | OpenTelemetry tracing | ❌ (not migrated) |
| `backend/production_config.py` | Config loading for legacy backend | ✅ Replaced by `app/config.py` |
| `backend/rate_limiter.py` | IP-based rate limiting | ✅ `app/core/rate_limiter.py` |
| `backend/healing_engine.py` | Auto-healing runtime engine | ❌ (functionality in `app/services/account_health.py`) |
| `backend/cryptomus.py` | Cryptomus API client | ❌ (not migrated) |
| `backend/debug_free_api.py` | Debug/free API key utilities | ❌ (not migrated) |

## Summary

- **Already migrated** (via `app/api/`): accounts, auth, broadcast, auto_reply, reply_macro, groups, folders, healing, admin, free_api_key, folder/smart_folders
- **Already migrated** (via `app/routers/`): guest_routes, stars_payments, draft_routes, trigger_routes, ai_admin
- **Needs migration**: health, runtime_inspector, cryptomus_payments, telegram_bot (webhook routes), monitoring, OpenTelemetry, healing_engine, cryptomus client
- **Not applicable** (deprecated/redesigned): account_runtime, runtime_manager, event_bus, models (legacy SQLite)
