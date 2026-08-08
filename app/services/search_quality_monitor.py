"""Search quality monitoring and automatic alerting for KB/RAG pipeline.

Detects low-quality retrieval and triggers alerts for admin review.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core.logging import get_logger
from app.models.ai_chat_v2 import AiChatMessageV2

logger = get_logger(__name__)

_KB_QUALITY_THRESHOLD = 0.5
_WEB_FALLBACK_THRESHOLD = 0.3
_ALERT_COOLDOWN_MINUTES = 30


async def evaluate_search_quality(
    db,
    kb_results: list[Any],
    web_results: list[dict[str, Any]] | None,
    query: str,
    tenant_id: str,
) -> dict[str, Any]:
    """Evaluate KB/web search quality and emit alerts if needed.

    Args:
        db: AsyncSession.
        kb_results: List of KB search results.
        web_results: Optional web search results.
        query: User query text.
        tenant_id: Tenant identifier.

    Returns:
        Dict with keys: quality_score, source_used, alert_triggered, reason.
    """
    now = datetime.now(timezone.utc)
    kb_score = max((r.score for r in kb_results), default=0.0) if kb_results else 0.0
    web_score = max((r.get("score", 0.0) for r in (web_results or [])), default=0.0)

    if kb_score >= _KB_QUALITY_THRESHOLD:
        quality = "high" if kb_score >= 0.75 else "medium"
        return {
            "quality_score": kb_score,
            "source_used": "kb",
            "alert_triggered": False,
            "reason": f"KB hit with score {kb_score:.0%}",
        }

    if web_results and web_score >= _WEB_FALLBACK_THRESHOLD:
        return {
            "quality_score": web_score,
            "source_used": "web",
            "alert_triggered": False,
            "reason": f"Web fallback with score {web_score:.0%}",
        }

    _maybe_alert(db, tenant_id, query, kb_score, web_score, now)
    return {
        "quality_score": kb_score,
        "source_used": "none",
        "alert_triggered": True,
        "reason": f"No KB/web results above threshold (KB={kb_score:.0%}, web={web_score:.0%})",
    }


def _maybe_alert(
    db,
    tenant_id: str,
    query: str,
    kb_score: float,
    web_score: float,
    now: datetime,
) -> None:
    """Emit a quality alert if cooldown has passed."""
    try:
        from app.models.system_setting import SystemSetting

        alert_key = f"kb_quality_alert:{tenant_id}"
        result = db.execute(
            __import__("sqlalchemy").select(SystemSetting).where(SystemSetting.key == alert_key).limit(1)
        )
        row = result.scalar_one_or_none()
        last_alert = datetime.fromisoformat(row.value) if row and row.value else None
        if last_alert and (now - last_alert).total_seconds() < _ALERT_COOLDOWN_MINUTES * 60:
            return

        logger.warning(
            "kb_quality_alert",
            tenant_id=tenant_id,
            query=query[:200],
            kb_score=kb_score,
            web_score=web_score,
        )
        if row:
            row.value = now.isoformat()
        else:
            db.add(SystemSetting(id=__import__("uuid").uuid4().hex, key=alert_key, value=now.isoformat()))
        db.commit()
    except Exception as exc:
        logger.debug("kb_quality_alert_failed", error=str(exc))


async def get_quality_alerts(db, tenant_id: str | None = None, hours: int = 24) -> list[dict[str, Any]]:
    """Return recent quality alerts from logs (best-effort).

    Since alerts are log-based, this returns empty and is reserved for
    future DB-backed alert storage.
    """
    return []
