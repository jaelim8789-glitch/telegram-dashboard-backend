from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.api.deps import Identity
from app.core.logging import get_logger
from app.database import async_session_maker
from app.models.account import Account
from app.models.message_log import MessageLog
from app.models.tenant import Lead, Tenant
from app.services.ai_analysis_service import analyze_text_report

logger = get_logger(__name__)

_REPORT_WINDOW_DAYS = 7
_cached_reports: dict[str, "GrowthCoachReport"] = {}
_running = False


@dataclass
class GrowthCoachMetrics:
    tenant_id: str
    window_days: int
    total_sent: int
    success_count: int
    failed_count: int
    reply_count: int
    best_hour_utc: int | None
    source_mix: dict[str, int]
    new_members: int
    click_rate_percent: float
    view_rate_percent: float


@dataclass
class GrowthCoachReport:
    tenant_id: str
    generated_at: str
    summary: str
    todos: list[str]
    metrics: dict


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _extract_todos(report_text: str) -> list[str]:
    todos: list[str] = []
    for raw in report_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        normalized = line.lstrip("-•0123456789. ").strip()
        if not normalized:
            continue
        if any(token in line for token in ("할 일", "추천", "액션", "실행")) and len(normalized) > 4:
            todos.append(normalized)
            continue
        if line[:2].isdigit() and "." in line[:4] and len(normalized) > 4:
            todos.append(normalized)
    dedup: list[str] = []
    for item in todos:
        if item not in dedup:
            dedup.append(item)
    return dedup[:5]


def _coach_system_prompt() -> str:
    return (
        "너는 텔레그램 마케팅 성장 코치다. 데이터 기반으로 오늘 바로 실행할 액션을 추천한다.\n"
        "반드시 한국어로 답하고, 첫 문단에 핵심 요약 2~3문장을 작성한다.\n"
        "그 다음 '오늘 할 일 5개'를 번호 목록(1~5)으로 작성한다.\n"
        "각 항목은 실행 가능한 문장 1개로 짧고 명확하게 작성한다.\n"
        "가능하면 발송 시간, 포맷(이미지/텍스트), 대상 세그먼트, 메시지 톤을 구체적으로 제시한다."
    )


async def _compute_metrics_for_tenant(tenant_id: str, window_days: int = _REPORT_WINDOW_DAYS) -> GrowthCoachMetrics:
    since = _utcnow_naive() - timedelta(days=window_days)
    async with async_session_maker() as db:
        account_rows = await db.execute(select(Account.id).where(Account.tenant_id == tenant_id))
        account_ids = [row[0] for row in account_rows.all()]

        if not account_ids:
            return GrowthCoachMetrics(
                tenant_id=tenant_id,
                window_days=window_days,
                total_sent=0,
                success_count=0,
                failed_count=0,
                reply_count=0,
                best_hour_utc=None,
                source_mix={},
                new_members=0,
                click_rate_percent=0.0,
                view_rate_percent=0.0,
            )

        summary_row = await db.execute(
            select(
                func.count(MessageLog.id),
                func.sum(func.cast(MessageLog.success, func.Integer)),
                func.sum(func.cast((MessageLog.source == "auto_reply"), func.Integer)),
            ).where(
                MessageLog.account_id.in_(account_ids),
                MessageLog.created_at >= since,
            )
        )
        total_sent, success_count, reply_count = summary_row.one()
        total_sent = int(total_sent or 0)
        success_count = int(success_count or 0)
        reply_count = int(reply_count or 0)
        failed_count = max(total_sent - success_count, 0)

        source_rows = await db.execute(
            select(MessageLog.source, func.count(MessageLog.id))
            .where(
                MessageLog.account_id.in_(account_ids),
                MessageLog.created_at >= since,
            )
            .group_by(MessageLog.source)
        )
        source_mix = {str(src or "unknown"): int(cnt or 0) for src, cnt in source_rows.all()}

        hour_rows = await db.execute(
            select(
                func.extract("hour", MessageLog.created_at),
                func.count(MessageLog.id),
                func.sum(func.cast(MessageLog.success, func.Integer)),
            )
            .where(
                MessageLog.account_id.in_(account_ids),
                MessageLog.created_at >= since,
            )
            .group_by(func.extract("hour", MessageLog.created_at))
        )
        best_hour_utc: int | None = None
        best_rate = -1.0
        for hour, attempted, successful in hour_rows.all():
            attempted_i = int(attempted or 0)
            if attempted_i <= 0:
                continue
            rate = (int(successful or 0) / attempted_i) * 100
            if rate > best_rate:
                best_rate = rate
                best_hour_utc = int(hour)

        new_members_row = await db.execute(
            select(func.count(Lead.id)).where(
                Lead.tenant_id == tenant_id,
                Lead.created_at >= since,
            )
        )
        new_members = int(new_members_row.scalar() or 0)

    if total_sent > 0:
        click_rate_percent = round((source_mix.get("link_click", 0) / total_sent) * 100, 2)
        view_rate_percent = round((source_mix.get("channel_view", 0) / total_sent) * 100, 2)
    else:
        click_rate_percent = 0.0
        view_rate_percent = 0.0

    return GrowthCoachMetrics(
        tenant_id=tenant_id,
        window_days=window_days,
        total_sent=total_sent,
        success_count=success_count,
        failed_count=failed_count,
        reply_count=reply_count,
        best_hour_utc=best_hour_utc,
        source_mix=source_mix,
        new_members=new_members,
        click_rate_percent=click_rate_percent,
        view_rate_percent=view_rate_percent,
    )


async def generate_growth_coach_for_tenant(tenant_id: str, *, window_days: int = _REPORT_WINDOW_DAYS) -> GrowthCoachReport:
    metrics = await _compute_metrics_for_tenant(tenant_id, window_days=window_days)
    user_prompt = "\n".join(
        [
            "아래 운영 데이터를 분석해.",
            "오늘 할 일 5개 추천해.",
            json.dumps(
                {
                    "window_days": metrics.window_days,
                    "total_sent": metrics.total_sent,
                    "success_count": metrics.success_count,
                    "failed_count": metrics.failed_count,
                    "reply_rate_percent": round((metrics.reply_count / max(metrics.total_sent, 1)) * 100, 2),
                    "best_hour_utc": metrics.best_hour_utc,
                    "new_members": metrics.new_members,
                    "click_rate_percent": metrics.click_rate_percent,
                    "view_rate_percent": metrics.view_rate_percent,
                    "source_mix": metrics.source_mix,
                },
                ensure_ascii=False,
            ),
            "조회수/클릭률 데이터가 충분하지 않다면 한계를 명시하고 대체 액션을 제안해.",
        ]
    )

    report_text, _ = await analyze_text_report(_coach_system_prompt(), user_prompt, max_anomalies=0)
    if report_text is None:
        report_text = "데이터 분석에 일시 실패했습니다. 오늘은 최근 성공률이 높은 시간대에 핵심 메시지를 1회 테스트하세요."

    todos = _extract_todos(report_text)
    if len(todos) < 5:
        defaults = [
            "성공률이 높은 시간대에 핵심 메시지 1건을 먼저 발송하세요.",
            "이미지 포함 버전과 텍스트 버전을 A/B로 테스트하세요.",
            "최근 실패 로그 상위 3개 원인을 정리하고 재시도 정책을 적용하세요.",
            "답장률이 높은 세그먼트에 후속 메시지를 예약하세요.",
            "신규 유입 대상 온보딩 문구를 1개 개선해 오늘 적용하세요.",
        ]
        for item in defaults:
            if item not in todos:
                todos.append(item)
            if len(todos) >= 5:
                break

    report = GrowthCoachReport(
        tenant_id=tenant_id,
        generated_at=_utcnow_naive().isoformat(),
        summary=report_text,
        todos=todos[:5],
        metrics={
            "window_days": metrics.window_days,
            "total_sent": metrics.total_sent,
            "success_count": metrics.success_count,
            "failed_count": metrics.failed_count,
            "reply_count": metrics.reply_count,
            "best_hour_utc": metrics.best_hour_utc,
            "source_mix": metrics.source_mix,
            "new_members": metrics.new_members,
            "click_rate_percent": metrics.click_rate_percent,
            "view_rate_percent": metrics.view_rate_percent,
        },
    )
    _cached_reports[tenant_id] = report
    return report


async def get_growth_coach_for_identity(identity: Identity) -> GrowthCoachReport:
    if not identity.tenant_id:
        raise ValueError("tenant_id is required")
    cached = _cached_reports.get(identity.tenant_id)
    if cached is not None:
        return cached
    return await generate_growth_coach_for_tenant(identity.tenant_id)


async def run_daily_growth_coach() -> None:
    global _running
    if _running:
        logger.info("growth_coach_skipped", reason="already_running")
        return
    _running = True
    try:
        async with async_session_maker() as db:
            rows = await db.execute(select(Tenant.id).where(Tenant.is_active.is_(True)))
            tenant_ids = [row[0] for row in rows.all()]

        for tenant_id in tenant_ids:
            try:
                await generate_growth_coach_for_tenant(tenant_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("growth_coach_tenant_failed", tenant_id=tenant_id, error=str(exc))
        logger.info("growth_coach_daily_completed", tenant_count=len(tenant_ids))
    finally:
        _running = False