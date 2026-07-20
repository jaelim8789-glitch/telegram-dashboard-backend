"""
No-Code Trigger/Action System.

트리거 → 조건 → 액션의 시각적 워크플로우 정의/실행.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import get_current_identity

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/triggers", tags=["triggers"])

DB_PATH = os.environ.get("ADMIN_DB_PATH", "data/admin.db")
_db_initialized = False
ALLOWED_RULE_FIELDS = {"name", "description", "trigger_type", "trigger_config", "actions", "is_active", "cooldown_seconds"}

TRIGGER_DEFS = [
    {"id": "message_received", "label": "메시지 수신", "icon": "MessageCircle", "desc": "새 메시지 도착",
     "params": [{"key": "keyword", "label": "키워드", "type": "string"}]},
    {"id": "member_joined", "label": "멤버 가입", "icon": "UserPlus", "desc": "새 멤버 가입", "params": []},
    {"id": "member_left", "label": "멤버 탈퇴", "icon": "UserMinus", "desc": "멤버 탈퇴", "params": []},
    {"id": "schedule", "label": "시간 예약", "icon": "Clock", "desc": "특정 시간 실행",
     "params": [{"key": "cron", "label": "Cron", "type": "string"}]},
    {"id": "broadcast_complete", "label": "발송 완료", "icon": "Send", "desc": "발송 완료 시", "params": []},
]

ACTION_DEFS = [
    {"id": "send_message", "label": "메시지 발송", "icon": "Send", "desc": "메시지 전송",
     "params": [{"key": "message", "label": "내용", "type": "textarea"}]},
    {"id": "send_ai_reply", "label": "AI 답변", "icon": "Bot", "desc": "AI 답변 생성",
     "params": [{"key": "prompt", "label": "프롬프트", "type": "textarea"}]},
    {"id": "notify_admin", "label": "관리자 알림", "icon": "Bell", "desc": "알림 전송",
     "params": [{"key": "message", "label": "내용", "type": "textarea"}]},
    {"id": "webhook", "label": "Webhook", "icon": "Globe", "desc": "외부 URL 호출",
     "params": [{"key": "url", "label": "URL", "type": "string"}]},
]


def _ensure_db() -> None:
    global _db_initialized
    if _db_initialized:
        return
    import os
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rules (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
                description TEXT DEFAULT '', is_active INTEGER DEFAULT 1,
                trigger_type TEXT NOT NULL, trigger_config TEXT DEFAULT '{}',
                actions TEXT DEFAULT '[]', cooldown_seconds INTEGER DEFAULT 0,
                run_count INTEGER DEFAULT 0, last_run_at TEXT,
                created_at TEXT DEFAULT '', updated_at TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trigger_logs (
                id TEXT PRIMARY KEY, rule_id TEXT NOT NULL,
                trigger_type TEXT NOT NULL, event_data TEXT DEFAULT '{}',
                actions_executed TEXT DEFAULT '[]', result TEXT DEFAULT 'success',
                error_message TEXT, executed_at TEXT DEFAULT ''
            )
        """)
        conn.commit()
    finally:
        conn.close()
    _db_initialized = True


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@router.get("/definitions/triggers")
async def list_triggers():
    return {"triggers": TRIGGER_DEFS}


@router.get("/definitions/actions")
async def list_actions():
    return {"actions": ACTION_DEFS}


@router.get("")
async def list_rules(identity=Depends(get_current_identity)):
    user_id = identity.user_id or identity.tenant_id or ""
    _ensure_db()
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT * FROM rules WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
        return {"rules": [{**dict(r), "trigger_config": json.loads(r["trigger_config"] or "{}"),
                           "actions": json.loads(r["actions"] or "[]")} for r in rows]}
    finally:
        conn.close()


@router.post("")
async def create_rule(body: dict, identity=Depends(get_current_identity)):
    user_id = identity.user_id or identity.tenant_id or ""
    _ensure_db()
    rule_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO rules (id,user_id,name,description,is_active,trigger_type,trigger_config,actions,cooldown_seconds,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rule_id, user_id, body.get("name","Rule")[:200], body.get("description","")[:500],
             1 if body.get("is_active",True) else 0, body.get("trigger_type",""),
             json.dumps(body.get("trigger_config",{})), json.dumps(body.get("actions",[])),
             body.get("cooldown_seconds",0), now, now),
        )
        conn.commit()
        return {"id": rule_id}
    finally:
        conn.close()


@router.get("/{rule_id}")
async def get_rule(rule_id: str, identity=Depends(get_current_identity)):
    _ensure_db()
    conn = _get_conn()
    try:
        r = conn.execute("SELECT * FROM rules WHERE id=? AND user_id=?", (rule_id, user_id)).fetchone()
        if not r:
            return {"error": "not found"}
        d = dict(r)
        d["trigger_config"] = json.loads(d.get("trigger_config","{}"))
        d["actions"] = json.loads(d.get("actions","[]"))
        return d
    finally:
        conn.close()


@router.put("/{rule_id}")
async def update_rule(rule_id: str, body: dict, identity=Depends(get_current_identity)):
    _ensure_db()
    now = datetime.now(timezone.utc).isoformat()
    updates, params = [], []
    for field in body:
        if field not in ALLOWED_RULE_FIELDS:
            continue
        val = body[field]
        if field in ("trigger_config", "actions"):
            val = json.dumps(val) if isinstance(val, (dict, list)) else val
        if field == "is_active":
            val = 1 if val else 0
        updates.append(f"{field}=?")
        params.append(val)
    if not updates:
        return {"updated": False}
    updates.append("updated_at=?")
    params.append(now); params.append(rule_id); params.append(user_id)
    conn = _get_conn()
    try:
        conn.execute(f"UPDATE rules SET {','.join(updates)} WHERE id=? AND user_id=?", params)
        conn.commit()
        return {"updated": True}
    finally:
        conn.close()


@router.delete("/{rule_id}")
async def delete_rule(rule_id: str, identity=Depends(get_current_identity)):
    _ensure_db()
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM rules WHERE id=? AND user_id=?", (rule_id, user_id))
        conn.commit()
        return {"deleted": True}
    finally:
        conn.close()


@router.post("/{rule_id}/toggle")
async def toggle_rule(rule_id: str, identity=Depends(get_current_identity)):
    _ensure_db()
    conn = _get_conn()
    try:
        conn.execute("UPDATE rules SET is_active = CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=? AND user_id=?",
                     (rule_id, user_id))
        conn.commit()
        r = conn.execute("SELECT is_active FROM rules WHERE id=?", (rule_id,)).fetchone()
        return {"is_active": bool(r["is_active"])} if r else {"error": "not found"}
    finally:
        conn.close()


@router.get("/stats/summary")
async def trigger_stats(identity=Depends(get_current_identity)):
    _ensure_db()
    conn = _get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) as c FROM rules WHERE user_id=?", (user_id,)).fetchone()["c"]
        active = conn.execute("SELECT COUNT(*) as c FROM rules WHERE user_id=? AND is_active=1", (user_id,)).fetchone()["c"]
        return {"total_rules": total, "active_rules": active}
    finally:
        conn.close()
