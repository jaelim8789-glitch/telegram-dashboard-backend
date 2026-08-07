"""AI 주간 품질 리포트 — 지난 N일간의 답변 품질/피드백/실패를 집계.

모델을 교체하거나 프롬프트를 손댄 뒤 \"품질이 나빠졌나?\"를 수치로 확인하기 위한
스크립트입니다. ai_chat_v2 의 assistant 메시지에 저장된 quality metadata와
사용자 피드백(feedback_score)을 집계하고, 실패(빈 답변/파싱 실패) 비율도 함께
뽑습니다.

Usage:
    python -m scripts.ai_weekly_quality_report --days 7 [--json]

Exit code 0 on success (report generated), 1 if the DB query fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.database import async_session_maker
from app.models.ai_chat_v2 import AiChatMessageV2
from app.models.ai_reply_v2 import AiReplySuggestionV2


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def run(days: int) -> dict:
    since = _utcnow_naive() - timedelta(days=days)
    report: dict = {
        "period_days": days,
        "generated_at": _utcnow_naive().isoformat(),
        "chat": {},
        "reply_suggestions": {},
    }

    async with async_session_maker() as db:
        # ── AI Chat v2 ──
        rows = (
            await db.execute(
                select(AiChatMessageV2)
                .where(
                    AiChatMessageV2.role == "assistant",
                    AiChatMessageV2.created_at >= since,
                )
                .order_by(AiChatMessageV2.created_at.desc())
                .limit(5000)
            )
        ).scalars().all()

        total = len(rows)
        empty = sum(1 for m in rows if not (m.content or "").strip())
        with_feedback = [m for m in rows if m.feedback_score is not None]
        scores = [m.feedback_score for m in with_feedback]
        quality_scores = []
        domains: dict[str, int] = {}
        for m in rows:
            for item in (m.memory_context or []):
                if isinstance(item, dict):
                    if item.get("quality_score") is not None:
                        quality_scores.append(int(item["quality_score"]))
                    dom = item.get("domain")
                    if dom:
                        domains[dom] = domains.get(dom, 0) + 1

        report["chat"] = {
            "total_answers": total,
            "empty_answers": empty,
            "empty_rate": round(empty / total, 4) if total else 0.0,
            "avg_feedback_score": round(sum(scores) / len(scores), 2) if scores else None,
            "feedback_count": len(scores),
            "avg_quality_score": round(sum(quality_scores) / len(quality_scores), 1) if quality_scores else None,
            "quality_scored_answers": len(quality_scores),
            "by_domain": domains,
            "negative_feedback": sum(1 for s in scores if s <= 2),
        }

        # ── AI Reply v2 (auto-reply suggestions) ──
        sugg = (
            await db.execute(
                select(AiReplySuggestionV2)
                .where(AiReplySuggestionV2.created_at >= since)
                .limit(2000)
            )
        ).scalars().all()
        sent = sum(1 for s in sugg if s.auto_reply_sent)
        report["reply_suggestions"] = {
            "total": len(sugg),
            "auto_sent": sent,
            "auto_sent_rate": round(sent / len(sugg), 4) if sugg else 0.0,
            "avg_response_time_ms": round(
                sum(s.response_time_ms or 0 for s in sugg) / len(sugg)
            ) if sugg else None,
        }

    return report


async def main() -> int:
    parser = argparse.ArgumentParser(description="AI weekly quality report")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    args = parser.parse_args()

    report = await run(args.days)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        c = report["chat"]
        r = report["reply_suggestions"]
        print(f"── AI 품질 리포트 (최근 {report['period_days']}일) ──")
        print(f"AI Chat 답변: {c['total_answers']}건 | 빈 답변: {c['empty_answers']}건 ({c['empty_rate']:.1%})")
        if c["avg_feedback_score"] is not None:
            print(f"사용자 피드백 평균: {c['avg_feedback_score']}점 (응답 {c['feedback_count']}건, 부정 {c['negative_feedback']}건)")
        if c["avg_quality_score"] is not None:
            print(f"내부 품질 점수 평균: {c['avg_quality_score']}점 ({c['quality_scored_answers']}건 스코어링)")
        if c["by_domain"]:
            print(f"도메인 분포: {c['by_domain']}")
        print(f"AI Reply 추천: {r['total']}건 | 자동 전송: {r['auto_sent']}건 ({r['auto_sent_rate']:.1%})"
              + (f" | 평균 응답: {r['avg_response_time_ms']}ms" if r["avg_response_time_ms"] else ""))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(main()))
