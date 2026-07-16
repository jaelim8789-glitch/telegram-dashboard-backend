"""AI 운영 자동화 — periodic AI-generated ops report (report-only, no actions).

Runs on a schedule (see app.scheduler.scheduler), gathers cross-tenant delivery
analytics via the existing app.services.delivery_analytics functions (same
data source the "운영 분석" dashboard and the on-demand /api/ai/analyze-delivery
endpoint use), and stores an AI-generated report via ai_analysis_service. This
module never takes any action (no account pause, no plan change, nothing) —
it only writes a row for an operator to read later via GET /api/ai/ops-reports.
"""

import json
from dataclasses import asdict

from app.api.deps import Identity
from app.core.logging import get_logger
from app.crud.ai_ops_report import create_report
from app.database import async_session_maker
from app.services.ai_analysis_service import DELIVERY_SYSTEM_PROMPT, analyze_text_report
from app.services.delivery_analytics import get_account_performance, get_failure_breakdown, get_summary

logger = get_logger(__name__)

_REPORT_WINDOW_DAYS = 7

# Cross-tenant aggregate view — matches the admin-only gate on GET
# /api/ai/ops-reports (this data spans every tenant, not just one).
_ADMIN_IDENTITY = Identity(kind="admin")


async def generate_and_store_ops_report() -> None:
    summary = await get_summary(_ADMIN_IDENTITY, days=_REPORT_WINDOW_DAYS)
    failures = await get_failure_breakdown(_ADMIN_IDENTITY, days=_REPORT_WINDOW_DAYS)
    accounts = await get_account_performance(_ADMIN_IDENTITY, days=_REPORT_WINDOW_DAYS)

    user_prompt = "\n".join(
        [
            f"[요약] {json.dumps(asdict(summary), ensure_ascii=False)}",
            f"[실패 분석] {json.dumps([asdict(f) for f in failures], ensure_ascii=False)}",
            f"[계정 성과] {json.dumps([asdict(a) for a in accounts], ensure_ascii=False)}",
            f"[분석 기간] 최근 {_REPORT_WINDOW_DAYS}일",
        ]
    )

    report, anomalies = await analyze_text_report(DELIVERY_SYSTEM_PROMPT, user_prompt)
    if report is None:
        logger.warning("ai_ops_report_generation_skipped", reason="deepseek_call_failed")
        return

    async with async_session_maker() as db:
        await create_report(db, report=report, anomalies_json=json.dumps(anomalies, ensure_ascii=False))

    logger.info("ai_ops_report_generated", anomaly_count=len(anomalies))
