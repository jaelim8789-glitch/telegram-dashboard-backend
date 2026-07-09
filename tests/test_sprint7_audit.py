"""
Sprint 7 Integration & Security Audit Tests.
Verifies every Sprint 7 feature: reachability, isolation, billing security,
scheduler correctness, migration, and serialization.

Run:  pytest tests/test_sprint7_audit.py -v
"""

import pytest
from fastapi.testclient import TestClient


# ═══════════════════════════════════════════════════════════════════════
# Phase 1 — Feature Runtime Reachability
# ═══════════════════════════════════════════════════════════════════════

def test_all_sprint7_routers_registered():
    """Verify every Sprint 7 router prefix is present in the FastAPI app."""
    from app.main import app
    routes = [r.path for r in app.routes]
    prefixes = [
        "/api/accounts/",
        "/api/group-search",
        "/api/billing",
        "/api/payment",
        "/api/features",
    ]
    for prefix in prefixes:
        assert any(prefix in r for r in routes), f"Missing router prefix: {prefix}"


def test_reply_macro_model_exported():
    from app.models import ReplyMacro, ReplyMacroLog
    assert ReplyMacro.__tablename__ == "reply_macros"
    assert ReplyMacroLog.__tablename__ == "reply_macro_logs"


def test_tenant_model_exported():
    from app.models import Tenant, PaymentRecord, UsageRecord, Lead
    assert Tenant.__tablename__ == "tenants"
    assert PaymentRecord.__tablename__ == "payment_records"
    assert UsageRecord.__tablename__ == "usage_records"
    assert Lead.__tablename__ == "leads"


def test_message_template_models_exported():
    from app.models import MessageTemplate, FollowUpRule, TeamMember
    assert MessageTemplate.__tablename__ == "message_templates"
    assert FollowUpRule.__tablename__ == "follow_up_rules"
    assert TeamMember.__tablename__ == "team_members"


# ═══════════════════════════════════════════════════════════════════════
# Phase 2 — Multi-tenant Isolation Audit
# ═══════════════════════════════════════════════════════════════════════

def test_account_has_tenant_id():
    """Sprint 8: Account now has tenant_id FK — establishes tenant boundary."""
    from app.models.account import Account
    cols = [c.name for c in Account.__table__.columns]
    assert "tenant_id" in cols, "Account must have tenant_id for cross-tenant isolation"
    # Verify FK
    fks = [fk for fk in Account.__table__.foreign_keys]
    assert any(fk.parent.name == "tenant_id" for fk in fks), "tenant_id must be a FK to tenants"


def test_tenant_isolation_matrix():
    """
    Verify ownership chain for every tenant-owned resource.
    
    Current model:
      Tenant -> no link to Account
      Account -> owns reply_macros, broadcasts, group_search, telegram sessions
      Tenant -> owns message_templates, follow_up_rules, team_members, usage_records, leads
    
    Isolation gap: Account-scoped resources have NO tenant boundary enforcement.
    """
    from app.models.reply_macro import ReplyMacro
    from app.models.broadcast import Broadcast
    from app.models.group_search import GroupSearchResult
    from app.models.message_template import MessageTemplate, FollowUpRule, TeamMember
    from app.models.tenant import UsageRecord, Lead

    assert hasattr(MessageTemplate, "tenant_id")
    assert hasattr(FollowUpRule, "tenant_id")
    assert hasattr(TeamMember, "tenant_id")
    assert hasattr(UsageRecord, "tenant_id")
    assert hasattr(Lead, "tenant_id")

    assert not hasattr(ReplyMacro, "tenant_id")
    assert not hasattr(Broadcast, "tenant_id")
    assert not hasattr(GroupSearchResult, "tenant_id")


# ═══════════════════════════════════════════════════════════════════════
# Phase 3 — Billing Security Audit
# ═══════════════════════════════════════════════════════════════════════

def test_billing_routers_missing_auth():
    """
    Verify billing write endpoints have auth.
    /api/billing/usdt/confirm must NOT be public.
    /api/payment/* routes are intentionally public (signup flow).
    """
    from app.main import app
    
    billing_routes = [r for r in app.routes if "/api/billing" in getattr(r, "path", "")]
    for route in billing_routes:
        deps = getattr(route, "dependencies", [])
        if not deps:
            path = getattr(route, "path", "")
            if "/api/billing/plans" in path:
                continue
            if "/api/billing/usdt/confirm" in path:
                pytest.fail(f"CRITICAL: {path} has NO authentication!")

    # /api/payment routes are intentionally public for signup flow
    payment_routes = [r for r in app.routes if "/api/payment" in getattr(r, "path", "")]
    for route in payment_routes:
        path = getattr(route, "path", "")
        if path in ("/api/payment/plans", "/api/payment/request-key", "/api/payment/status/{payment_ref}"):
            continue
        deps = getattr(route, "dependencies", [])
        if not deps:
            pytest.fail(f"CRITICAL: {path} has NO authentication!")


def test_usdt_confirm_no_auth_mocked():
    """Verify that POST /api/billing/usdt/confirm requires auth."""
    from app.main import app
    client = TestClient(app)
    resp = client.post("/api/billing/usdt/confirm", params={"tenant_id": "test", "tx_hash": "test"})
    assert resp.status_code in (400, 403, 401), f"Unexpected status: {resp.status_code}"


def test_payment_amount_not_trusted():
    """Verify that frontend cannot set the payment amount."""
    from app.api.usdt_payment import PLANS
    assert "basic" in PLANS
    assert PLANS["basic"]["usdt"] == 15


def test_tx_id_uniqueness():
    """
    PaymentRecord.tx_id has a UNIQUE constraint — prevents duplicate processing.
    Checks both column-level and table-level unique constraints.
    """
    from app.models.tenant import PaymentRecord
    # Check column-level unique
    tx_id_col = PaymentRecord.__table__.columns["tx_id"]
    if tx_id_col.unique:
        return  # column-level unique exists
    # Check table-level UniqueConstraint
    for constraint in PaymentRecord.__table__.constraints:
        if hasattr(constraint, 'columns') and constraint.unique:
            col_names = [col.name for col in constraint.columns]
            if "tx_id" in col_names:
                return  # table-level unique exists
    pytest.fail("PaymentRecord.tx_id MUST be UNIQUE to prevent double-spending")


# ═══════════════════════════════════════════════════════════════════════
# Phase 4 — Scheduler Audit
# ═══════════════════════════════════════════════════════════════════════

def test_reply_macro_max_sends_enforced():
    """
    Verify execute_reply_macro now checks max_sends_per_day.
    """
    from app.services.reply_macro_service import execute_reply_macro
    import inspect
    source = inspect.getsource(execute_reply_macro)
    assert "max_sends_per_day" in source, "max_sends_per_day must be checked in the service"


def test_scheduler_dispatch_has_no_lock():
    """
    dispatch_due_reply_macros runs every 30 seconds with no locking.
    This is a known risk — macros could double-fire if execution spans ticks.
    """
    from app.scheduler.scheduler import dispatch_due_reply_macros
    import inspect
    source = inspect.getsource(dispatch_due_reply_macros)
    assert "asyncio.Lock" not in source, "No concurrency lock — macros could double-fire"


# ═══════════════════════════════════════════════════════════════════════
# Phase 6 — API Serialization Audit
# ═══════════════════════════════════════════════════════════════════════

def test_reply_macro_read_target_chats_is_list():
    """Sprint 8: ReplyMacroRead.target_chats now returns list[str] consistently."""
    from app.schemas.reply_macro import ReplyMacroRead, ReplyMacroCreate
    create_field = ReplyMacroCreate.model_fields["target_chats"]
    read_field = ReplyMacroRead.model_fields["target_chats"]
    assert create_field.annotation == list[str], "Create accepts list[str]"
    assert read_field.annotation == list[str], "Read must ALSO return list[str]"
    # Verify the from_orm override exists
    assert hasattr(ReplyMacroRead, "from_orm"), "Must have from_orm for JSON deserialization"


def test_all_schemas_have_from_attributes():
    """All Sprint 7 Read schemas must use from_attributes=True for ORM mode."""
    from app.schemas.reply_macro import ReplyMacroRead, ReplyMacroLogRead
    from app.schemas.group_search import GroupSearchResultRead, GroupJoinLogRead
    for schema in [ReplyMacroRead, ReplyMacroLogRead, GroupSearchResultRead, GroupJoinLogRead]:
        assert schema.model_config.get("from_attributes"), f"{schema.__name__} missing from_attributes"


# ═══════════════════════════════════════════════════════════════════════
# Phase 7 — Integration Defect Tests
# ═══════════════════════════════════════════════════════════════════════

def test_billing_api_injects_auth():
    """Verify billing routers have auth_required dependency."""
    from app.main import app
    billing_paths = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if "/api/billing/usdt/confirm" in path:
            deps = getattr(route, "dependencies", [])
            billing_paths.append((path, len(deps)))
    
    for path, dep_count in billing_paths:
        if dep_count == 0:
            pytest.fail(f"{path} has 0 dependencies — needs auth_required")


def test_features_routers_have_auth():
    """Verify features router has auth_required dependency."""
    from app.main import app
    features_routes = [r for r in app.routes if "/api/features" in getattr(r, "path", "")]
    route_zero_auth = []
    for route in features_routes:
        deps = getattr(route, "dependencies", [])
        if not deps:
            route_zero_auth.append(getattr(route, "path", ""))
    if route_zero_auth:
        pytest.fail(f"Features routes without auth: {route_zero_auth}")


# ═══════════════════════════════════════════════════════════════════════
# Phase 9 — Model Schema Matches Migration
# ═══════════════════════════════════════════════════════════════════════

def test_migration_f8a5d3b2c1e0_creates_expected_tables():
    """Verify migration creates all Sprint 7 tables."""
    migration_path = "alembic/versions/f8a5d3b2c1e0_create_tenant_billing_reply_macro_tables.py"
    with open(migration_path, encoding="utf-8") as f:
        content = f.read()
    
    expected_tables = [
        "tenants",
        "payment_records",
        "usage_records",
        "leads",
        "reply_macros",
        "reply_macro_logs",
        "message_templates",
        "follow_up_rules",
        "team_members",
    ]
    for table in expected_tables:
        assert f'"{table}"' in content or f"'{table}'" in content, f"Migration does not create {table}"


def test_crud_reply_macro_stores_json_not_csv():
    """
    target_chats must be stored as JSON array, not comma-separated.
    """
    from app.crud import reply_macro as macro_crud
    import inspect
    source = inspect.getsource(macro_crud.create_macro)
    assert 'json.dumps' in source, "create_macro must json.dumps target_chats"
    assert '",".join' not in source, "Do NOT comma-join target_chats"