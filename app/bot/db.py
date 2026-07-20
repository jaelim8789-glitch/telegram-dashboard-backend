"""
Bot-owned SQLite tables.

Additive only: these tables live inside the existing admin.db file
(same file AdminPlatform and free_api_key already use) but are created
via CREATE TABLE IF NOT EXISTS and never alter an existing table's
schema. Nothing in this module touches runtime.db (accounts, broadcasts,
auto_reply_rules, reply_macros) — that stays fully owned by the
Telethon account runtime.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

DB_PATH = os.environ.get("ADMIN_DB_PATH", "data/admin.db")


def init_bot_tables() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_sessions (
                chat_id TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                telegram_user_id INTEGER,
                telegram_username TEXT,
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_admin_notify_log (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                message TEXT DEFAULT '',
                delivered INTEGER DEFAULT 0,
                created_at TEXT DEFAULT ''
            )
        """)
        conn.commit()
    finally:
        conn.close()


def upsert_session(
    chat_id: str,
    token: str,
    telegram_user_id: int | None = None,
    telegram_username: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute(
            """INSERT INTO bot_sessions (chat_id, token, telegram_user_id, telegram_username, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 token = excluded.token,
                 telegram_user_id = excluded.telegram_user_id,
                 telegram_username = excluded.telegram_username,
                 updated_at = excluded.updated_at""",
            (chat_id, token, telegram_user_id, telegram_username, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_session(chat_id: str) -> dict[str, Any] | None:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM bot_sessions WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def log_admin_notify(event_type: str, message: str, delivered: bool) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute(
            """INSERT INTO bot_admin_notify_log (id, event_type, message, delivered, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), event_type, message, 1 if delivered else 0, now),
        )
        conn.commit()
    finally:
        conn.close()


# ── AI Employee Tables ───────────────────────────────────────────────


def init_ai_tables() -> None:
    """Create AI Employee (AiEmployee) tables if they don't exist.

    Call this alongside init_bot_tables() during startup.
    """
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_group_style_profiles (
                chat_id INTEGER PRIMARY KEY,
                style_profile_id TEXT NOT NULL DEFAULT 'default',
                available_actions TEXT NOT NULL DEFAULT '["번역","요약","날씨","뉴스","도움말"]',
                updated_at TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_custom_commands (
                name TEXT PRIMARY KEY,
                system_prompt TEXT NOT NULL,
                created_at TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_scheduled_messages (
                id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                parse_mode TEXT DEFAULT 'Markdown',
                send_at TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                error_message TEXT,
                created_at TEXT DEFAULT '',
                sent_at TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


# ── StyleProfile CRUD ────────────────────────────────────────────────


def upsert_group_style_profile(
    chat_id: int,
    style_profile_id: str,
    available_actions: list[str] | None = None,
) -> None:
    """Set or update a group's style profile."""
    now = datetime.now(timezone.utc).isoformat()
    actions_json = json.dumps(available_actions) if available_actions else None
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        if actions_json:
            conn.execute(
                """INSERT INTO ai_group_style_profiles (chat_id, style_profile_id, available_actions, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     style_profile_id = excluded.style_profile_id,
                     available_actions = excluded.available_actions,
                     updated_at = excluded.updated_at""",
                (chat_id, style_profile_id, actions_json, now),
            )
        else:
            conn.execute(
                """INSERT INTO ai_group_style_profiles (chat_id, style_profile_id, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     style_profile_id = excluded.style_profile_id,
                     updated_at = excluded.updated_at""",
                (chat_id, style_profile_id, now),
            )
        conn.commit()
    finally:
        conn.close()


def get_group_style_profile(chat_id: int) -> dict[str, Any] | None:
    """Get a group's style profile."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM ai_group_style_profiles WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row:
            result = dict(row)
            if result.get("available_actions"):
                try:
                    result["available_actions"] = json.loads(result["available_actions"])
                except (json.JSONDecodeError, TypeError):
                    pass
            return result
        return None
    finally:
        conn.close()


# ── Scheduled Messages CRUD ──────────────────────────────────────────


def insert_scheduled_message(
    chat_id: int,
    text: str,
    send_at: str,
    parse_mode: str = "Markdown",
) -> str:
    """Insert a scheduled message and return its ID."""
    msg_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute(
            """INSERT INTO ai_scheduled_messages (id, chat_id, text, parse_mode, send_at, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (msg_id, chat_id, text, parse_mode, send_at, now),
        )
        conn.commit()
    finally:
        conn.close()
    return msg_id


def get_pending_scheduled_messages() -> list[dict[str, Any]]:
    """Get all pending messages that are due to be sent."""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM ai_scheduled_messages WHERE status = 'pending' AND send_at <= ? ORDER BY send_at ASC",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_scheduled_message_sent(msg_id: str) -> None:
    """Mark a scheduled message as sent."""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute(
            "UPDATE ai_scheduled_messages SET status = 'sent', sent_at = ? WHERE id = ?",
            (now, msg_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_scheduled_message_failed(msg_id: str, error: str) -> None:
    """Mark a scheduled message as failed."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute(
            "UPDATE ai_scheduled_messages SET status = 'failed', error_message = ? WHERE id = ?",
            (error, msg_id),
        )
        conn.commit()
    finally:
        conn.close()


def cancel_scheduled_message(msg_id: str) -> bool:
    """Cancel a pending scheduled message. Returns True if cancelled, False if not found."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        cur = conn.execute(
            "UPDATE ai_scheduled_messages SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (msg_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_scheduled_messages(
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Get scheduled messages, optionally filtered by status."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM ai_scheduled_messages WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ai_scheduled_messages ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Custom Commands CRUD ─────────────────────────────────────────────


def save_custom_command(name: str, system_prompt: str) -> None:
    """Save a custom command to the database (upsert)."""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute(
            """INSERT INTO ai_custom_commands (name, system_prompt, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 system_prompt = excluded.system_prompt,
                 created_at = excluded.created_at""",
            (name.lower(), system_prompt, now),
        )
        conn.commit()
    finally:
        conn.close()


def load_custom_commands() -> list[dict[str, Any]]:
    """Load all custom commands from the database."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM ai_custom_commands ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_custom_command(name: str) -> bool:
    """Delete a custom command. Returns True if deleted."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        cur = conn.execute(
            "DELETE FROM ai_custom_commands WHERE name = ?", (name.lower(),),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_custom_command_names() -> list[str]:
    """Get all registered custom command names."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        rows = conn.execute(
            "SELECT name FROM ai_custom_commands ORDER BY name ASC"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def delete_group_style_profile(chat_id: int) -> bool:
    """Delete a group's style profile. Returns True if deleted, False if not found."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        cur = conn.execute(
            "DELETE FROM ai_group_style_profiles WHERE chat_id = ?", (chat_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
