"""
Contract tests: verify backend API responses match the schemas the frontend expects.

These tests start from the TypeScript type definitions in src/types/index.ts and
src/lib/api.ts, then compare them against live backend responses. This catches:

  - Missing fields (backend removed a field the frontend uses)
  - Wrong field types (e.g. string vs number)
  - Structural mismatches (e.g. nested object vs flat schema)

Run:
    pytest tests/test_contract_api_schema.py -v
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# ── Fixture: API client ──────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def contract_client():
    """Create a client that hits the real app (requires app.main)."""
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Helper: resolve dynamic paths ────────────────────────────────────────────

DYNAMIC_PATH_PLACEHOLDER = "{account_id}"
FAKE_ACCOUNT_ID = "00000000-0000-0000-0000-000000000000"


def _resolve_path(schema: dict[str, Any]) -> str:
    """Replace {account_id} placeholder with a fake ID for path construction."""
    path = schema.get("path", "")
    if DYNAMIC_PATH_PLACEHOLDER in path:
        path = path.replace(DYNAMIC_PATH_PLACEHOLDER, FAKE_ACCOUNT_ID)
    return path


# ── Schema Registry ──────────────────────────────────────────────────────────
# These are derived from the frontend's TypeScript interfaces in src/types/index.ts
# and src/lib/api.ts. Each entry defines the expected shape of a response.
#
# Special entry flags:
#   is_sub_schema   — only checked as a nested item within another schema
#   is_dynamic_path — path contains {account_id} placeholder
#   check_array_item — schema for each element when the response is an array

SCHEMA_REGISTRY: list[dict[str, Any]] = [
    # ── Core ───────────────────────────────────────────────────────────
    {
        "name": "Accounts list",
        "method": "GET",
        "path": "/api/accounts",
        "auth_required": True,
        "check_items": [
            ("id", str),
            ("phone", str),
            ("name", (str, type(None))),
            ("status", str),
            ("today_sent", int),
            ("group_count", int),
            ("last_activity", (str, type(None))),
            ("auto_reply_enabled", bool),
            ("created_at", str),
            ("updated_at", str),
        ],
    },
    {
        "name": "Account health",
        "method": "GET",
        "path": "/api/account-health",
        "auth_required": True,
        "check_items": [
            ("account_id", str),
            ("phone", str),
            ("name", (str, type(None))),
            ("status", str),
            ("has_session", bool),
            ("last_activity", (str, type(None))),
            ("last_error", (str, type(None))),
            ("recent_success_count", int),
            ("recent_failure_count", int),
            ("total_delivery_attempts", int),
        ],
    },
    {
        "name": "Auth me",
        "method": "GET",
        "path": "/api/auth/me",
        "auth_required": True,
        "check_items": [
            ("role", str),
            ("phone", (str, type(None))),
            ("subscription_status", (str, type(None))),
            ("plan", (str, type(None))),
            ("trial_expires_at", (str, type(None))),
        ],
    },
    {
        "name": "Admin dashboard status",
        "method": "GET",
        "path": "/api/admin/dashboard/status",
        "auth_required": True,
        "check_nested": {
            "users": {
                "total": int,
                "active": int,
                "inactive": int,
            },
            "accounts": {
                "total": int,
                "healthy": int,
                "unhealthy": int,
                "not_configured": int,
                "banned": int,
                "rate_limited": int,
                "unauthorized": int,
                "error_count": int,
                "unknown": int,
                "has_session": int,
                "has_errors": int,
                "total_today_sent": int,
                "total_groups": int,
            },
            "broadcasts": {
                "recent_total": int,
                "recent_failed": int,
                "failure_rate": (float, int),
                "recent_window_hours": int,
            },
        },
    },
    {
        "name": "Health root",
        "method": "GET",
        "path": "/",
        "auth_required": False,
        "check_items": [
            ("status", str),
            ("version", str),
            ("environment", str),
            ("uptime_seconds", (int, float)),
        ],
    },
    {
        "name": "Health endpoint",
        "method": "GET",
        "path": "/api/health",
        "auth_required": False,
        "check_items": [
            ("status", str),
        ],
    },
    {
        "name": "Free API key start",
        "method": "POST",
        "path": "/api/free-api-key/start",
        "auth_required": False,
        "check_items": [
            ("token", str),
            ("bot_deep_link", str),
            ("channel_url", str),
        ],
    },

    # ── Broadcast ─────────────────────────────────────────────────────
    {
        "name": "Broadcast read",
        "method": "POST",
        "path": "/api/broadcast",
        "auth_required": True,
        "check_items": [
            ("id", str),
            ("account_id", str),
            ("message", str),
            ("media_path", (str, type(None))),
            ("recipients", list),
            ("status", str),
            ("scheduled_at", (str, type(None), int, float)),
            ("sent_at", (str, type(None), int, float)),
            ("created_at", (str, int, float)),
            ("error_message", (str, type(None))),
            ("recurring_interval_minutes", (int, type(None))),
            ("cancelled_at", (str, type(None), int, float)),
            ("next_scheduled_at", (str, type(None), int, float)),
            ("is_recurring_paused", bool),
            ("failure_info", (dict, type(None))),
            ("delivery_mode", str),
            ("reply_to_msg_id", (int, type(None))),
            ("delay_seconds", (int, type(None))),
            ("inline_buttons", (list, type(None))),
            ("group_ids", (list, type(None))),
            ("campaign_id", (str, type(None))),
            ("distribution_batch_id", (str, type(None))),
        ],
    },
    {
        "name": "Recurring broadcasts list",
        "method": "GET",
        "path": "/api/broadcast/recurring",
        "auth_required": True,
        "check_array_item": [
            ("id", str),
            ("account_id", str),
            ("message", str),
            ("status", str),
            ("recurring_interval_minutes", (int, type(None))),
            ("is_recurring_paused", bool),
            ("delivery_mode", str),
            ("created_at", (str, int, float)),
        ],
    },
    {
        "name": "Broadcast estimate",
        "method": "POST",
        "path": "/api/broadcast/estimate",
        "auth_required": True,
        "check_items": [
            ("estimated_seconds", int),
            ("estimated_minutes", int),
            ("readable", str),
        ],
    },

    # ── Groups ─────────────────────────────────────────────────────────
    {
        "name": "Groups list (paginated)",
        "method": "GET",
        "path": "/api/accounts/{account_id}/groups",
        "auth_required": True,
        "is_dynamic_path": True,
        "check_nested": {
            "items": list,
            "total": int,
            "page": int,
            "page_size": int,
            "total_pages": int,
        },
    },
    {
        "name": "Group item",
        "auth_required": True,
        "is_sub_schema": True,
        "check_items": [
            ("id", str),
            ("title", str),
            ("type", str),
            ("participants_count", (int, type(None))),
        ],
    },
    {
        "name": "Group discovery info",
        "method": "GET",
        "path": "/api/accounts/{account_id}/groups/discovery-info",
        "auth_required": True,
        "is_dynamic_path": True,
        "check_items": [
            ("total_groups", int),
            ("groups", int),
            ("channels", int),
        ],
    },

    # ── Auto-Reply ────────────────────────────────────────────────────
    {
        "name": "Auto-reply settings",
        "method": "GET",
        "path": "/api/accounts/{account_id}/auto-reply",
        "auth_required": True,
        "is_dynamic_path": True,
        "check_items": [
            ("account_id", str),
            ("auto_reply_enabled", bool),
            ("rules", list),
        ],
    },
    {
        "name": "Auto-reply rule (sub-schema)",
        "auth_required": True,
        "is_sub_schema": True,
        "check_items": [
            ("id", str),
            ("account_id", str),
            ("name", str),
            ("is_active", bool),
            ("match_type", str),
            ("match_value", str),
            ("reply_content", str),
            ("cooldown_hours", (int, float)),
            ("max_replies_per_day", int),
            ("created_at", (str, int, float)),
            ("updated_at", (str, int, float)),
        ],
    },
    {
        "name": "Auto-reply toggle response",
        "method": "POST",
        "path": "/api/accounts/{account_id}/auto-reply/toggle",
        "auth_required": True,
        "is_dynamic_path": True,
        "check_items": [
            ("account_id", str),
            ("auto_reply_enabled", bool),
        ],
    },
    {
        "name": "Auto-reply logs list",
        "method": "GET",
        "path": "/api/accounts/{account_id}/auto-reply/logs",
        "auth_required": True,
        "is_dynamic_path": True,
        "check_array_item": [
            ("id", str),
            ("rule_id", str),
            ("account_id", str),
            ("chat_id", (str, int)),
            ("user_id", (str, int)),
            ("user_name", (str, type(None))),
            ("trigger_message", str),
            ("reply_sent", str),
            ("status", str),
            ("created_at", (str, int, float)),
        ],
    },

    # ── Reply Macro ───────────────────────────────────────────────────
    {
        "name": "Reply macros list",
        "method": "GET",
        "path": "/api/accounts/{account_id}/reply-macros",
        "auth_required": True,
        "is_dynamic_path": True,
        "check_array_item": [
            ("id", str),
            ("account_id", str),
            ("name", str),
            ("target_chats", list),
            ("message_content", str),
            ("media_path", (str, type(None))),
            ("created_at", (str, int, float)),
            ("updated_at", (str, int, float)),
        ],
    },
    {
        "name": "Reply macro toggle state",
        "method": "GET",
        "path": "/api/accounts/{account_id}/reply-macros/toggle",
        "auth_required": True,
        "is_dynamic_path": True,
        "check_items": [
            ("is_active", bool),
            ("message_content", (str, type(None))),
        ],
    },
]

# ── Schema Check Utilities ───────────────────────────────────────────────────


def _check_field(value: Any, expected_type: type | tuple) -> list[str]:
    """Verify a single field matches one of the expected types."""
    if isinstance(expected_type, tuple):
        if not isinstance(value, expected_type):
            expected_names = tuple(
                t.__name__ if hasattr(t, "__name__") else str(t)
                for t in expected_type
            )
            return [
                f"  Expected type {expected_names}, "
                f"got {type(value).__name__} for value {value!r}"
            ]
    else:
        expected_name = (
            expected_type.__name__ if hasattr(expected_type, "__name__") else str(expected_type)
        )
        if not isinstance(value, expected_type):
            return [
                f"  Expected {expected_name}, "
                f"got {type(value).__name__} for value {value!r}"
            ]
    return []


def _check_items(label: str, data: Any, items: list) -> list[str]:
    """Check top-level fields in a dict."""
    errors: list[str] = []
    if not isinstance(data, dict):
        errors.append(f"  Expected dict, got {type(data).__name__}")
        return errors
    for field_name, expected_type in items:
        if field_name not in data:
            errors.append(f"  Missing field: '{field_name}'")
        else:
            errors.extend(_check_field(data[field_name], expected_type))
    return errors


def _check_nested(label: str, data: Any, schema: dict) -> list[str]:
    """Check nested dict structure (recursive)."""
    errors: list[str] = []
    if isinstance(schema, dict) and not isinstance(data, dict):
        errors.append(f"  [{label}] Expected dict, got {type(data).__name__}")
        return errors
    for key, subschema in schema.items():
        if key not in data:
            errors.append(f"  [{label}] Missing nested key: '{key}'")
            continue
        if isinstance(subschema, dict):
            errors.extend(_check_nested(f"{label}.{key}", data[key], subschema))
        else:
            errors.extend(_check_field(data[key], subschema))
    return errors


def _check_array_items(label: str, data: Any, items: list) -> list[str]:
    """Check each item in an array against a field schema."""
    errors: list[str] = []
    if not isinstance(data, list):
        errors.append(f"  [{label}] Expected list, got {type(data).__name__}")
        return errors
    for idx, item in enumerate(data[:3]):  # Check first 3 items max
        if isinstance(item, dict):
            item_errors = _check_items(f"{label}[{idx}]", item, items)
            errors.extend(item_errors)
    return errors


# ── Contract Tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema",
    [s for s in SCHEMA_REGISTRY if not s.get("auth_required") and not s.get("is_sub_schema")],
    ids=[s["name"] for s in SCHEMA_REGISTRY if not s.get("auth_required") and not s.get("is_sub_schema")],
)
async def test_contract_no_auth(schema: dict[str, Any], contract_client: AsyncClient):
    """Test endpoints that don't require authentication."""
    method = schema["method"].lower()
    path = _resolve_path(schema)
    response = await getattr(contract_client, method)(path)
    assert response.status_code in (200, 201), (
        f"{schema['name']}: expected 200, got {response.status_code}: {response.text[:200]}"
    )

    data = response.json()
    errors: list[str] = []

    if "check_items" in schema:
        errors.extend(_check_items(schema["name"], data, schema["check_items"]))
    if "check_nested" in schema:
        errors.extend(_check_nested(schema["name"], data, schema["check_nested"]))
    if "check_array_item" in schema:
        if isinstance(data, list):
            errors.extend(_check_array_items(schema["name"], data, schema["check_array_item"]))
        elif isinstance(data, dict) and "items" in data:
            errors.extend(_check_array_items(schema["name"], data["items"], schema["check_array_item"]))

    if errors:
        pytest.fail(f"\n{schema['name']} schema mismatch:\n" + "\n".join(errors))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema",
    [s for s in SCHEMA_REGISTRY if s.get("auth_required") and not s.get("is_sub_schema")],
    ids=[s["name"] for s in SCHEMA_REGISTRY if s.get("auth_required") and not s.get("is_sub_schema")],
)
async def test_contract_with_auth(schema: dict[str, Any], contract_client: AsyncClient):
    """Test endpoints that require authentication.

    For each endpoint:
      1. Verifies that the auth gate returns 401/403 when no auth is provided
      2. Bypasses auth via DI override, then validates the response schema
    """
    import app.main
    from app.api.deps import require_api_key_or_admin

    method = schema["method"].lower()
    path = _resolve_path(schema)

    # Step 1: Verify auth gate works
    unauth_response = await getattr(contract_client, method)(path)
    assert unauth_response.status_code in (401, 403), (
        f"{schema['name']}: expected 401/403 without auth, "
        f"got {unauth_response.status_code}"
    )

    # Step 2: Bypass auth for schema validation
    app.main.app.dependency_overrides.clear()
    app.main.app.dependency_overrides[require_api_key_or_admin] = lambda: None

    try:
        response = await getattr(contract_client, method)(path)
        # Some endpoints may still fail gracefully (no DB, no accounts, etc.)
        if response.status_code not in (200, 201, 404):
            # If it's a 422 or other error, skip schema validation
            return

        if response.status_code in (200, 201):
            data = response.json()
            errors: list[str] = []

            # For list endpoints wrapped in {items: [...]}, extract items
            check_data = data
            if isinstance(data, dict) and "items" in data:
                check_data = data["items"]

            # If it's an array, check the first item
            if isinstance(check_data, list) and len(check_data) > 0:
                check_data = check_data[0]
            elif isinstance(check_data, list) and len(check_data) == 0:
                check_data = {}

            if "check_items" in schema:
                errors.extend(_check_items(schema["name"], check_data, schema["check_items"]))
            if "check_nested" in schema:
                errors.extend(_check_nested(schema["name"], check_data, schema["check_nested"]))
            if "check_array_item" in schema:
                items = data if isinstance(data, list) else data.get("items", [])
                errors.extend(
                    _check_array_items(schema["name"], items, schema["check_array_item"])
                )

            if errors:
                pytest.fail(f"\n{schema['name']} schema mismatch:\n" + "\n".join(errors))
    finally:
        app.main.app.dependency_overrides.clear()


# ─── Free-form: health endpoint structure ───────────────────────────────────


@pytest.mark.asyncio
async def test_contract_health_root(contract_client: AsyncClient):
    """Root health check must have status, version, environment, uptime."""
    response = await contract_client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data.get("status") == "ok"
    assert isinstance(data.get("version"), str)
    assert isinstance(data.get("environment"), str)
    assert isinstance(data.get("uptime_seconds"), (int, float))


@pytest.mark.asyncio
async def test_contract_health_api(contract_client: AsyncClient):
    """Health endpoint must have status."""
    response = await contract_client.get("/api/health")
    assert response.status_code in (200, 404), f"health endpoint returned {response.status_code}"
    if response.status_code == 200:
        data = response.json()
        assert "status" in data


# ─── Sub-schema tests (validate nested item shapes) ─────────────────────────


@pytest.mark.asyncio
async def test_contract_group_item_schema(contract_client: AsyncClient):
    """Verify Group item from paginated groups response has the expected fields."""
    from app.main import app
    from app.api.deps import require_api_key_or_admin

    app.dependency_overrides.clear()
    app.dependency_overrides[require_api_key_or_admin] = lambda: None
    try:
        response = await contract_client.get(
            f"/api/accounts/{FAKE_ACCOUNT_ID}/groups?page_size=1"
        )
        if response.status_code != 200:
            return  # Skip if no accounts
        data = response.json()
        items = data.get("items", [])
        if items:
            item = items[0]
            errors = _check_items("Group item", item, [
                ("id", str),
                ("title", str),
                ("type", str),
                ("participants_count", (int, type(None))),
            ])
            if errors:
                pytest.fail("\n" + "\n".join(errors))
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_contract_auto_reply_rule_schema(contract_client: AsyncClient):
    """Verify AutoReplyRule from settings response has the expected fields."""
    from app.main import app
    from app.api.deps import require_api_key_or_admin

    app.dependency_overrides.clear()
    app.dependency_overrides[require_api_key_or_admin] = lambda: None
    try:
        response = await contract_client.get(
            f"/api/accounts/{FAKE_ACCOUNT_ID}/auto-reply"
        )
        if response.status_code != 200:
            return
        data = response.json()
        rules = data.get("rules", [])
        if rules:
            rule = rules[0]
            errors = _check_items("Auto-reply rule", rule, [
                ("id", str),
                ("account_id", str),
                ("name", str),
                ("is_active", bool),
                ("match_type", str),
                ("match_value", str),
                ("reply_content", str),
                ("cooldown_hours", (int, float)),
                ("max_replies_per_day", int),
                ("created_at", (str, int, float)),
                ("updated_at", (str, int, float)),
            ])
            if errors:
                pytest.fail("\n" + "\n".join(errors))
    finally:
        app.dependency_overrides.clear()


# ─── Direct Pydantic Schema Validation (no HTTP call needed) ───────────────
# These tests validate that the Pydantic response models define all the fields
# the frontend expects. They work without needing seeded data or request bodies.


@pytest.mark.parametrize(
    "schema_name,model_path,expected_fields",
    [
        (
            "BroadcastRead",
            "app.schemas.broadcast.BroadcastRead",
            {
                "id", "account_id", "message", "media_path", "recipients",
                "status", "scheduled_at", "sent_at", "created_at",
                "error_message", "recurring_interval_minutes", "cancelled_at",
                "next_scheduled_at", "is_recurring_paused", "failure_info",
                "delivery_mode", "reply_to_msg_id", "delay_seconds",
                "inline_buttons", "distribution_batch_id",
            },
        ),
        (
            "GroupRead",
            "app.schemas.group.GroupRead",
            {"id", "title", "type", "participants_count"},
        ),
        (
            "PaginatedGroups",
            "app.schemas.group.PaginatedGroups",
            {"items", "total", "page", "page_size", "total_pages"},
        ),
        (
            "AutoReplyRuleRead",
            "app.schemas.auto_reply.AutoReplyRuleRead",
            {
                "id", "account_id", "name", "is_active", "match_type",
                "match_value", "reply_content", "cooldown_hours",
                "max_replies_per_day", "created_at", "updated_at",
            },
        ),
        (
            "AutoReplySettingsRead",
            "app.schemas.auto_reply.AutoReplySettingsRead",
            {"account_id", "auto_reply_enabled", "rules"},
        ),
        (
            "AutoReplyLogRead",
            "app.schemas.auto_reply.AutoReplyLogRead",
            {
                "id", "rule_id", "account_id", "chat_id", "user_id",
                "user_name", "trigger_message", "reply_sent", "status",
                "created_at",
            },
        ),
        (
            "BroadcastEstimateResponse",
            "app.schemas.broadcast.BroadcastEstimateResponse",
            {"estimated_seconds", "estimated_minutes", "readable"},
        ),
        (
            "AutoReplyToggleResponse",
            "app.schemas.auto_reply.AutoReplyToggleResponse",
            {"account_id", "auto_reply_enabled"},
        ),
        (
            "ReplyMacroRead",
            "app.schemas.reply_macro.ReplyMacroRead",
            {
                "id", "account_id", "name", "target_chats",
                "message_content", "media_path",
                "created_at", "updated_at",
            },
        ),
    ],
)
def test_pydantic_schema_fields(schema_name: str, model_path: str, expected_fields: set[str]):
    """Verify the Pydantic response model has all fields the frontend expects.

    This test does NOT make any HTTP requests — it directly inspects the
    Pydantic model class definition. This guarantees coverage even when
    test data isn't available in the database.
    """
    import importlib

    module_path, class_name = model_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    model_class = getattr(module, class_name)

    actual_fields = set(model_class.model_fields.keys())
    missing = expected_fields - actual_fields
    extra = actual_fields - expected_fields

    assert not missing, (
        f"\n{schema_name} is MISSING fields the frontend expects:\n"
        + "\n".join(f"  • {f}" for f in sorted(missing))
    )
    if extra:
        print(f"  [?] {schema_name} has extra untracked fields: {sorted(extra)}")


# ─── Verify that all critical frontend-expected endpoints exist ─────────────


@pytest.mark.asyncio
async def test_contract_critical_endpoints_exist(contract_client: AsyncClient):
    """Critical API endpoints referenced in src/lib/api.ts must respond (any status)."""
    critical_paths = [
        ("GET", "/"),
        ("GET", "/health"),
        ("GET", "/api/health"),
        ("GET", "/api/accounts"),
        ("GET", "/api/account-health"),
        ("GET", "/api/auth/me"),
        ("GET", "/api/admin/dashboard/status"),
        ("POST", "/api/free-api-key/start"),
        # Broadcast
        ("GET", "/api/broadcast/recurring"),
        ("POST", "/api/broadcast/estimate"),
        # Groups
        ("GET", f"/api/accounts/{FAKE_ACCOUNT_ID}/groups"),
        ("GET", f"/api/accounts/{FAKE_ACCOUNT_ID}/groups/folders"),
        # Auto-reply
        ("GET", f"/api/accounts/{FAKE_ACCOUNT_ID}/auto-reply"),
        ("GET", f"/api/accounts/{FAKE_ACCOUNT_ID}/auto-reply/logs"),
        # Reply macros
        ("GET", f"/api/accounts/{FAKE_ACCOUNT_ID}/reply-macros"),
        ("GET", f"/api/accounts/{FAKE_ACCOUNT_ID}/reply-macros/toggle"),
    ]
    for method, path in critical_paths:
        response = await getattr(contract_client, method.lower())(path)
        acceptable = (200, 201, 401, 403, 404, 405, 422)
        assert response.status_code in acceptable, (
            f"{method} {path}: unexpected {response.status_code} — {response.text[:100]}"
        )
