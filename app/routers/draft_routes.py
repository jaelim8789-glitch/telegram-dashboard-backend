"""
AI Draft + Human Approval Router.

워크플로우:
1. AI가 콘텐츠 생성 → draft 저장 (status="draft")
2. 사용자가 draft 검토 (리스트 조회, 미리보기)
3. 사용자가 승인/수정/거절
4. 승인된 draft → 자동 발송 예약 또는 즉시 발송

Content Studio가 AI 생성 → 이 라우터가 Draft 관리 → SendTab이 발송
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_current_identity
from app.runtime_manager import RuntimeManager
from app.schemas.broadcast import BroadcastCreate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/drafts", tags=["drafts"])

DB_PATH = os.environ.get("ADMIN_DB_PATH", "data/admin.db")

# ── DB 초기화 ──────────────────────────────────────────────────────


def _init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                account_id TEXT,
                title TEXT DEFAULT '',
                content TEXT NOT NULL,
                content_type TEXT DEFAULT 'custom',
                status TEXT DEFAULT 'draft',  -- draft | approved | rejected | scheduled | sent
                source TEXT DEFAULT 'manual',  -- manual | ai_chat | content_studio | template
                ai_model TEXT,
                tokens_used INTEGER DEFAULT 0,
                scheduled_at TEXT,
                sent_at TEXT,
                feedback TEXT,
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_drafts_user
            ON drafts(user_id, status, created_at)
        """)
        conn.commit()
    finally:
        conn.close()


_db_initialized = False


def _ensure_db() -> None:
    global _db_initialized
    if not _db_initialized:
        _init_db()
        _db_initialized = True


ALLOWED_DRAFT_FIELDS = {"title", "content", "content_type", "account_id"}


# ── Draft CRUD ─────────────────────────────────────────────────────


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@router.get("")
async def list_drafts(
    status: str | None = None,
    limit: int = 50,
    identity=Depends(get_current_identity),
):
    """Draft 목록 조회 (상태별 필터 가능)."""
    _ensure_db()
    conn = _get_conn()
    try:
        query = "SELECT * FROM drafts WHERE user_id = ?"
        params: list[Any] = [user_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        drafts = []
        for r in rows:
            d = dict(r)
            drafts.append(d)
        return {"drafts": drafts}
    finally:
        conn.close()


@router.post("")
async def create_draft(
    body: dict,
    identity=Depends(get_current_identity),
):
    """Draft 생성 (AI 생성 콘텐츠 저장)."""
    draft_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO drafts
               (id, user_id, account_id, title, content, content_type,
                source, ai_model, tokens_used, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                draft_id,
                user_id,
                body.get("account_id"),
                body.get("title", "")[:200],
                body.get("content", ""),
                body.get("content_type", "custom"),
                body.get("source", "manual"),
                body.get("ai_model"),
                body.get("tokens_used", 0),
                now,
                now,
            ),
        )
        conn.commit()
        return {"id": draft_id, "status": "draft", "created_at": now}
    finally:
        conn.close()


@router.get("/{draft_id}")
async def get_draft(
    draft_id: str,
    identity=Depends(get_current_identity),
):
    """단일 Draft 조회."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM drafts WHERE id = ? AND user_id = ?",
            (draft_id, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Draft not found")
        return dict(row)
    finally:
        conn.close()


@router.patch("/{draft_id}")
async def update_draft(
    draft_id: str,
    body: dict,
    identity=Depends(get_current_identity),
):
    """Draft 수정 (내용/제목/발송시간 변경)."""
    now = datetime.now(timezone.utc).isoformat()
    updates = []
    params: list[Any] = []

    for field in body:
        if field not in ALLOWED_DRAFT_FIELDS:
            continue
        updates.append(f"{field} = ?")
        params.append(body[field])

    updates.append("updated_at = ?")
    params.append(now)
    params.append(draft_id)
    params.append(user_id)

    conn = _get_conn()
    try:
        conn.execute(
            f"UPDATE drafts SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
            params,
        )
        conn.commit()
        return {"updated": True, "id": draft_id}
    finally:
        conn.close()


@router.post("/{draft_id}/approve")
async def approve_draft(
    draft_id: str,
    body: dict | None = None,
    identity=Depends(get_current_identity),
):
    """Draft 승인 → 즉시 발송 또는 예약.

    body.recipients — 발송 대상 그룹 ID 목록 (선택)
    body.account_id — 발송 계정 ID (선택, draft에 없을 경우)
    body.scheduled_at — 예약 발송 시간 (선택)
    body.feedback — 피드백 (선택)
    """
    now = datetime.now(timezone.utc).isoformat()
    scheduled_at = body.get("scheduled_at") if body else None
    recipients = body.get("recipients") if body else None
    override_account_id = body.get("account_id") if body else None

    new_status = "scheduled" if scheduled_at else "approved"
    feedback = body.get("feedback") if body else None

    conn = _get_conn()
    try:
        # 소유권 확인
        row = conn.execute(
            "SELECT * FROM drafts WHERE id = ? AND user_id = ?",
            (draft_id, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Draft not found")

        draft = dict(row)
        if draft["status"] not in ("draft",):
            raise HTTPException(status_code=400, detail=f"Cannot approve draft with status '{draft['status']}'")

        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [new_status, now]

        if scheduled_at:
            updates.append("scheduled_at = ?")
            params.append(scheduled_at)
        if feedback:
            updates.append("feedback = ?")
            params.append(feedback)

        params.append(draft_id)
        params.append(user_id)

        conn.execute(
            f"UPDATE drafts SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
            params,
        )
        conn.commit()

        logger.info(
            "[draft] approved: id=%s user=%s → %s",
            draft_id, user_id, new_status,
        )

        # 승인된 draft → Broadcast 자동 생성
        broadcast_id = None
        effective_account_id = override_account_id or draft.get("account_id")
        if effective_account_id and draft.get("content"):
            try:
                manager = RuntimeManager.get_instance()
                broadcast_input = BroadcastCreate(
                    account_id=effective_account_id,
                    message=draft["content"],
                    recipients=recipients or [],
                    scheduled_at=scheduled_at,
                )
                broadcast = await manager.create_broadcast(broadcast_input)
                broadcast_id = broadcast.id
                logger.info(
                    "[draft] broadcast auto-created: draft=%s broadcast=%s recipients=%d",
                    draft_id, broadcast_id, len(recipients or []),
                )
            except Exception as e:
                logger.exception(
                    "[draft] failed to auto-create broadcast: %s", e,
                )

        return {
            "id": draft_id,
            "status": new_status,
            "scheduled_at": scheduled_at,
            "broadcast_id": broadcast_id,
            "recipients_count": len(recipients) if recipients else 0,
        }
    finally:
        conn.close()


@router.post("/{draft_id}/reject")
async def reject_draft(
    draft_id: str,
    body: dict | None = None,
    identity=Depends(get_current_identity),
):
    """Draft 거절 + 피드백."""
    now = datetime.now(timezone.utc).isoformat()
    feedback = body.get("feedback") if body else ""

    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM drafts WHERE id = ? AND user_id = ?",
            (draft_id, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Draft not found")

        conn.execute(
            "UPDATE drafts SET status = 'rejected', feedback = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (feedback, now, draft_id, user_id),
        )
        conn.commit()

        logger.info("[draft] rejected: id=%s user=%s", draft_id, user_id)
        return {"id": draft_id, "status": "rejected"}
    finally:
        conn.close()


@router.delete("/{draft_id}")
async def delete_draft(
    draft_id: str,
    identity=Depends(get_current_identity),
):
    """Draft 삭제."""
    conn = _get_conn()
    try:
        conn.execute(
            "DELETE FROM drafts WHERE id = ? AND user_id = ?",
            (draft_id, user_id),
        )
        conn.commit()
        return {"deleted": True}
    finally:
        conn.close()


@router.post("/batch/approve")
async def batch_approve_drafts(
    body: dict,
    identity=Depends(get_current_identity),
):
    """Draft 일괄 승인. body.draft_ids: list[str]

    개별 approve와 동일하게 status 변경 + Broadcast 자동 생성.
    """
    draft_ids = body.get("draft_ids", [])
    if not draft_ids:
        raise HTTPException(status_code=400, detail="draft_ids required")
    if len(draft_ids) > 50:
        raise HTTPException(status_code=400, detail="Max 50 drafts per batch")

    now = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    try:
        placeholders = ",".join(["?"] * len(draft_ids))
        # Only approve drafts that belong to user AND are in 'draft' status
        rows = conn.execute(
            f"SELECT id, status, content, account_id FROM drafts WHERE id IN ({placeholders}) AND user_id = ?",
            [*draft_ids, user_id],
        ).fetchall()

        approved = 0
        broadcast_count = 0
        for r in rows:
            if r["status"] == "draft":
                conn.execute(
                    "UPDATE drafts SET status='approved', updated_at=? WHERE id=?",
                    (now, r["id"]),
                )
                approved += 1
        conn.commit()

        # 승인된 draft → Broadcast 자동 생성 (개별 approve와 동일한 로직)
        manager = RuntimeManager.get_instance()
        for r in rows:
            if r["status"] == "draft" and r["account_id"] and r["content"]:
                try:
                    broadcast_input = BroadcastCreate(
                        account_id=r["account_id"],
                        message=r["content"],
                        recipients=[],
                        scheduled_at=None,
                    )
                    await manager.create_broadcast(broadcast_input)
                    broadcast_count += 1
                except Exception as e:
                    logger.exception("[draft] batch approve broadcast failed for %s: %s", r["id"], e)

        logger.info(
            "[draft] batch approve: user=%s requested=%d approved=%d broadcasts=%d",
            user_id, len(draft_ids), approved, broadcast_count,
        )
        return {"approved": approved, "broadcasts_created": broadcast_count, "total": len(draft_ids)}
    finally:
        conn.close()


@router.post("/batch/reject")
async def batch_reject_drafts(
    body: dict,
    identity=Depends(get_current_identity),
):
    """Draft 일괄 거절. body.draft_ids: list[str]"""
    draft_ids = body.get("draft_ids", [])
    if not draft_ids:
        raise HTTPException(status_code=400, detail="draft_ids required")
    if len(draft_ids) > 50:
        raise HTTPException(status_code=400, detail="Max 50 drafts per batch")

    now = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    try:
        placeholders = ",".join(["?"] * len(draft_ids))
        rows = conn.execute(
            f"SELECT id, status FROM drafts WHERE id IN ({placeholders}) AND user_id = ?",
            [*draft_ids, user_id],
        ).fetchall()

        rejected = 0
        for r in rows:
            if r["status"] == "draft":
                conn.execute(
                    "UPDATE drafts SET status='rejected', updated_at=? WHERE id=?",
                    (now, r["id"]),
                )
                rejected += 1
        conn.commit()

        logger.info("[draft] batch reject: user=%s requested=%d rejected=%d", user_id, len(draft_ids), rejected)
        return {"rejected": rejected, "total": len(draft_ids)}
    finally:
        conn.close()


@router.post("/batch/delete")
async def batch_delete_drafts(
    body: dict,
    identity=Depends(get_current_identity),
):
    """Draft 일괄 삭제. body.draft_ids: list[str]"""
    draft_ids = body.get("draft_ids", [])
    if not draft_ids:
        raise HTTPException(status_code=400, detail="draft_ids required")
    if len(draft_ids) > 50:
        raise HTTPException(status_code=400, detail="Max 50 drafts per batch")

    conn = _get_conn()
    try:
        placeholders = ",".join(["?"] * len(draft_ids))
        conn.execute(
            f"DELETE FROM drafts WHERE id IN ({placeholders}) AND user_id = ?",
            [*draft_ids, user_id],
        )
        conn.commit()

        logger.info("[draft] batch delete: user=%s count=%d", user_id, len(draft_ids))
        return {"deleted": len(draft_ids)}
    finally:
        conn.close()


@router.get("/stats/summary")
async def draft_summary(identity=Depends(get_current_identity)):
    """Draft 통계 요약."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT status, COUNT(*) as cnt
               FROM drafts WHERE user_id = ?
               GROUP BY status""",
            (user_id,),
        ).fetchall()
        stats = {r["status"]: r["cnt"] for r in rows}
        return {
            "draft": stats.get("draft", 0),
            "approved": stats.get("approved", 0),
            "rejected": stats.get("rejected", 0),
            "scheduled": stats.get("scheduled", 0),
            "sent": stats.get("sent", 0),
            "total": sum(stats.values()),
        }
    finally:
        conn.close()
