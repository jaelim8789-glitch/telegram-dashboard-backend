# AI_RULES.md

## Tech Stack

- Python 3 async backend built with FastAPI and served by Uvicorn.
- Pydantic v2 and `pydantic-settings` for request/response schemas and environment-based configuration.
- SQLAlchemy 2.x async ORM with `asyncpg` and PostgreSQL 16 for persistence.
- Alembic for all database schema migrations.
- Telethon for Telegram user-account/MTProto operations; `python-telegram-bot` for optional bot interactions.
- APScheduler and FastAPI background tasks for in-process scheduled and asynchronous work; there is no Redis or separate task queue.
- `httpx` for asynchronous HTTP integrations, with Beautiful Soup for HTML parsing where required.
- `structlog` for structured application logging and `cryptography`/PyJWT for encryption and authentication.
- pytest, pytest-asyncio, pytest-cov, and HTTPX test clients for automated testing.
- Docker Compose runs the FastAPI backend, PostgreSQL, the separate Next.js frontend, and nginx reverse proxy.

## Library and Architecture Rules

- Keep API route handlers in `app/api/` or the existing `app/routers/` area. Route handlers should validate input, enforce dependencies/authorization, call services or CRUD functions, and return typed Pydantic responses; do not place substantial business logic in routes.
- Use Pydantic models in `app/schemas/` for API boundaries and SQLAlchemy models in `app/models/` for persistence. Do not return ORM models directly from public APIs unless an existing typed response model explicitly supports it.
- Use SQLAlchemy 2.x async APIs with `AsyncSession` from `app.database.get_db`. Never add synchronous database drivers or blocking ORM calls. Keep reusable data-access operations in `app/crud/`.
- Create every schema change as an Alembic revision in `alembic/versions/`; never rely on `create_all`, ad-hoc startup DDL, or manual production database edits.
- Put business and integration logic in `app/services/`. Use Telethon only for Telegram user-account/MTProto actions and `python-telegram-bot` only for bot API features; do not mix the two clients' responsibilities.
- Use the shared `httpx`-based patterns for outbound HTTP calls. Do not introduce `requests` or other blocking network clients inside async code.
- Use APScheduler for recurring or scheduled in-process jobs and FastAPI background tasks for short request-triggered work. Do not add Redis, Celery, or another queue unless the architecture is explicitly changed.
- Use `app.core.logging.get_logger`/structlog for logs. Emit structured event names and fields; do not use `print`, log secrets, API keys, JWTs, Telegram sessions, verification codes, or full sensitive payloads.
- Use existing security helpers and FastAPI dependencies for authentication, authorization, tenant isolation, rate limiting, encryption, and JWT handling. Never implement custom cryptography or bypass tenant-scoped queries.
- Use `pytest` with async tests for new behavior. Add focused tests under `tests/`, use existing fixtures, mock external Telegram/payment/SMS/AI services, and never require live third-party credentials.

## General Change Rules

- Follow the existing module structure and naming conventions; prefer small, focused changes over broad refactors.
- Read configuration through `app.config.settings`; do not hard-code credentials, deployment URLs, or environment-specific values.
- Preserve the single-process deployment model and graceful startup behavior unless a requested change explicitly revises the architecture.
- Treat all external input as untrusted: validate it at API boundaries, use ORM parameterization, and avoid exposing internal exception details.
- Update relevant tests and documentation whenever API contracts, configuration, migrations, or deployment behavior change.
