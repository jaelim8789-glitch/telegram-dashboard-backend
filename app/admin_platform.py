"""Minimal admin_platform adapter for telegram-dashboard-backend.

This provides just enough of the legacy AdminPlatform/Plan/AuditAction
surface for the ported bot routers to function. It is intentionally
narrow — the real tenant/admin logic lives in app.api.admin, app.crud,
and app.models; this module only bridges the old import paths.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("ADMIN_DB_PATH", "data/admin.db")


class Role(str, Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    USER = "user"
    API_KEY = "api_key"
    READ_ONLY = "read_only"


class Plan(str, Enum):
    FREE = "free"
    PRO = "pro"
    TEAM = "team"
    LIFETIME = "lifetime"


class Feature(str, Enum):
    BROADCAST = "broadcast"
    AUTO_REPLY = "auto_reply"
    REPLY_MACRO = "reply_macro"
    SCHEDULED_SEND = "scheduled_send"
    API_ACCESS = "api_access"
    WEBHOOKS = "webhooks"
    CUSTOM_TEMPLATES = "custom_templates"
    ANALYTICS = "analytics"
    AUDIT_LOG = "audit_log"
    HEALING_ENGINE = "healing_engine"
    PRIORITY_SUPPORT = "priority_support"
    WHITE_LABEL = "white_label"
    TEAM_MEMBERS = "team_members"
    SSO = "sso"
    CUSTOM_RATE_LIMITS = "custom_rate_limits"
    DEDICATED_INFRA = "dedicated_infrastructure"


class AuditAction(str, Enum):
    ACCOUNT_CREATED = "account.created"
    ACCOUNT_DELETED = "account.deleted"
    ACCOUNT_UPDATED = "account.updated"
    USER_LOGIN = "user.login"
    USER_LOGOUT = "user.logout"
    API_KEY_CREATED = "api_key.created"
    API_KEY_REVOKED = "api_key.revoked"
    PLAN_CHANGED = "plan.changed"
    TRIAL_STARTED = "trial.started"
    TRIAL_EXPIRED = "trial.expired"
    SUBSCRIPTION_CREATED = "subscription.created"
    SUBSCRIPTION_CANCELLED = "subscription.cancelled"
    PAYMENT_SUCCEEDED = "payment.succeeded"
    PAYMENT_FAILED = "payment.failed"
    INVOICE_CREATED = "invoice.created"
    ADMIN_ACTION = "admin.action"
    SETTINGS_CHANGED = "settings.changed"
    FEATURE_TOGGLED = "feature.toggled"
    USER_SUSPENDED = "user.suspended"
    USER_ACTIVATED = "user.activated"
    SYSTEM_CONFIG_CHANGED = "system.config_changed"
    MAINTENANCE_MODE = "system.maintenance"


@dataclass
class PlanDefinition:
    name: str
    max_accounts: int
    max_groups_per_account: int
    daily_send_limit: int
    daily_auto_reply_limit: int
    max_team_members: int
    features: set[Feature]
    price_monthly_cents: int
    price_yearly_cents: int
    api_rate_limit: int
    priority_support: bool = False
    audit_log_retention_days: int = 7
    daily_limit: int = 0
    feature_flags: dict[str, bool] = field(default_factory=dict)


PLANS: dict[str, PlanDefinition] = {
    Plan.FREE: PlanDefinition(
        name="Free",
        max_accounts=1,
        max_groups_per_account=50,
        daily_send_limit=100,
        daily_auto_reply_limit=50,
        max_team_members=1,
        features={
            Feature.BROADCAST, Feature.AUTO_REPLY, Feature.REPLY_MACRO,
            Feature.API_ACCESS, Feature.ANALYTICS,
        },
        price_monthly_cents=0,
        price_yearly_cents=0,
        api_rate_limit=30,
        audit_log_retention_days=3,
        daily_limit=100,
        feature_flags={"can_export": False, "can_webhook": False},
    ),
    Plan.PRO: PlanDefinition(
        name="Pro",
        max_accounts=10,
        max_groups_per_account=500,
        daily_send_limit=5000,
        daily_auto_reply_limit=2500,
        max_team_members=5,
        features={
            Feature.BROADCAST, Feature.AUTO_REPLY, Feature.REPLY_MACRO,
            Feature.SCHEDULED_SEND, Feature.API_ACCESS, Feature.WEBHOOKS,
            Feature.CUSTOM_TEMPLATES, Feature.ANALYTICS, Feature.AUDIT_LOG,
            Feature.HEALING_ENGINE, Feature.PRIORITY_SUPPORT,
        },
        price_monthly_cents=9999,
        price_yearly_cents=99990,
        api_rate_limit=120,
        priority_support=True,
        audit_log_retention_days=30,
        daily_limit=1000,
        feature_flags={"can_export": True, "can_webhook": True, "bulk_operations": True},
    ),
    Plan.TEAM: PlanDefinition(
        name="Team",
        max_accounts=50,
        max_groups_per_account=2000,
        daily_send_limit=50000,
        daily_auto_reply_limit=25000,
        max_team_members=20,
        features={
            Feature.BROADCAST, Feature.AUTO_REPLY, Feature.REPLY_MACRO,
            Feature.SCHEDULED_SEND, Feature.API_ACCESS, Feature.WEBHOOKS,
            Feature.CUSTOM_TEMPLATES, Feature.ANALYTICS, Feature.AUDIT_LOG,
            Feature.HEALING_ENGINE, Feature.PRIORITY_SUPPORT,
            Feature.TEAM_MEMBERS,
        },
        price_monthly_cents=29999,
        price_yearly_cents=299990,
        api_rate_limit=300,
        priority_support=True,
        audit_log_retention_days=60,
        daily_limit=5000,
        feature_flags={"can_export": True, "can_webhook": True, "bulk_operations": True, "sso": False},
    ),
    Plan.LIFETIME: PlanDefinition(
        name="Lifetime",
        max_accounts=100,
        max_groups_per_account=5000,
        daily_send_limit=100000,
        daily_auto_reply_limit=50000,
        max_team_members=50,
        features={
            Feature.BROADCAST, Feature.AUTO_REPLY, Feature.REPLY_MACRO,
            Feature.SCHEDULED_SEND, Feature.API_ACCESS, Feature.WEBHOOKS,
            Feature.CUSTOM_TEMPLATES, Feature.ANALYTICS, Feature.AUDIT_LOG,
            Feature.HEALING_ENGINE, Feature.PRIORITY_SUPPORT,
            Feature.WHITE_LABEL, Feature.TEAM_MEMBERS, Feature.SSO,
            Feature.CUSTOM_RATE_LIMITS, Feature.DEDICATED_INFRA,
        },
        price_monthly_cents=199999,
        price_yearly_cents=0,
        api_rate_limit=1000,
        priority_support=True,
        audit_log_retention_days=365,
        daily_limit=0,
        feature_flags={"can_export": True, "can_webhook": True, "bulk_operations": True, "sso": True, "white_label": True},
    ),
}


_PLAN_ALIASES: dict[str, str] = {
    "starter": "pro",
    "professional": "pro",
    "enterprise": "team",
    "premium": "pro",
}


def resolve_plan(plan_name: str | None) -> str:
    if not plan_name:
        return Plan.FREE
    if plan_name in PLANS:
        return plan_name
    return _PLAN_ALIASES.get(plan_name.lower(), Plan.FREE)


def _get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class AdminDB:
    def __init__(self, db_path: str = DB_PATH) -> None:
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    email TEXT,
                    phone TEXT,
                    role TEXT NOT NULL DEFAULT 'user',
                    plan TEXT NOT NULL DEFAULT 'free',
                    is_active INTEGER DEFAULT 1,
                    is_suspended INTEGER DEFAULT 0,
                    trial_started_at TEXT,
                    trial_ends_at TEXT,
                    subscription_id TEXT,
                    subscription_status TEXT DEFAULT 'inactive',
                    stripe_customer_id TEXT,
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT '',
                    last_login_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    key_hash TEXT NOT NULL,
                    key_prefix TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    permissions TEXT DEFAULT 'read',
                    plan TEXT DEFAULT 'free',
                    feature_flags TEXT DEFAULT '{}',
                    max_accounts INTEGER DEFAULT 0,
                    daily_limit INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    last_used_at TEXT,
                    expires_at TEXT,
                    created_at TEXT DEFAULT '',
                    usage_count INTEGER DEFAULT 0,
                    usage_reset_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    user_id TEXT,
                    username TEXT,
                    action TEXT NOT NULL,
                    resource_type TEXT,
                    resource_id TEXT,
                    details TEXT DEFAULT '{}',
                    ip_address TEXT,
                    user_agent TEXT,
                    success INTEGER DEFAULT 1
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    plan TEXT NOT NULL,
                    status TEXT DEFAULT 'active',
                    current_period_start TEXT,
                    current_period_end TEXT,
                    cancel_at_period_end INTEGER DEFAULT 0,
                    stripe_subscription_id TEXT,
                    stripe_price_id TEXT,
                    trial_start TEXT,
                    trial_end TEXT,
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS usage_records (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    messages_sent INTEGER DEFAULT 0,
                    auto_replies_sent INTEGER DEFAULT 0,
                    broadcasts_created INTEGER DEFAULT 0,
                    api_calls INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT '',
                    UNIQUE(user_id, date)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS feature_overrides (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    feature TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT '',
                    UNIQUE(user_id, feature)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS invoices (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    subscription_id TEXT,
                    amount_cents INTEGER NOT NULL,
                    currency TEXT DEFAULT 'usd',
                    status TEXT DEFAULT 'pending',
                    stripe_invoice_id TEXT,
                    paid_at TEXT,
                    due_date TEXT,
                    created_at TEXT DEFAULT ''
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        return _get_conn()


class AdminPlatform:
    _instance: AdminPlatform | None = None

    @classmethod
    def get_instance(cls) -> AdminPlatform:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self.db = AdminDB()

    def _audit(
        self,
        user_id: str,
        username: str,
        action: AuditAction,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        try:
            conn = _get_conn()
            conn.execute(
                """INSERT INTO audit_logs
                   (id, timestamp, user_id, username, action, resource_type, resource_id, details, success)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    str(uuid.uuid4()),
                    datetime.now(timezone.utc).isoformat(),
                    user_id,
                    username,
                    action.value if isinstance(action, AuditAction) else action,
                    resource_type,
                    resource_id,
                    json.dumps(details or {}),
                ),
            )
            conn.commit()
        except Exception:
            logger.debug("[admin_platform] audit log failed", exc_info=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def change_plan(self, user_id: str, new_plan: str) -> dict[str, Any]:
        conn = _get_conn()
        try:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE users SET plan = ?, updated_at = ? WHERE id = ?",
                (new_plan, now, user_id),
            )
            conn.commit()
            self._audit(user_id, "", AuditAction.PLAN_CHANGED, "user", user_id, {"new_plan": new_plan})
            return {}
        finally:
            conn.close()

    def create_subscription(self, user_id: str, plan: str) -> None:
        conn = _get_conn()
        try:
            conn.execute(
                """INSERT INTO subscriptions
                   (id, user_id, plan, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'active', ?, ?)""",
                (str(uuid.uuid4()), user_id, plan, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    def record_usage(self, user_id: str, api_calls: int = 0, **kwargs: Any) -> None:
        conn = _get_conn()
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """INSERT INTO usage_records
                   (id, user_id, date, api_calls, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, date) DO UPDATE SET
                   api_calls = api_calls + ?""",
                (str(uuid.uuid4()), user_id, today, api_calls, now, api_calls),
            )
            conn.commit()
        finally:
            conn.close()

    def create_invoice(
        self,
        user_id: str,
        amount_cents: int,
        stripe_invoice_id: str,
    ) -> None:
        conn = _get_conn()
        try:
            conn.execute(
                """INSERT INTO invoices
                   (id, user_id, amount_cents, stripe_invoice_id, status, currency, created_at)
                   VALUES (?, ?, ?, ?, 'pending', 'usd', ?)""",
                (
                    str(uuid.uuid4()),
                    user_id,
                    amount_cents,
                    stripe_invoice_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
