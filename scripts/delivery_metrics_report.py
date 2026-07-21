from __future__ import annotations

import argparse
import asyncio
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import async_session_maker
from app.models.message_log import MessageLog


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class MetricsSnapshot:
    hours: int
    source: str | None
    account_id: str | None
    generated_at: str
    attempts_total: int
    attempts_success: int
    attempts_failed: int
    success_rate_percent: float
    unique_recipients: int
    unique_broadcasts: int
    throughput_attempts_per_min: float
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    latency_max_ms: float | None
    top_statuses: list[tuple[str, int]]
    top_error_messages: list[tuple[str, int]]


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    sorted_vals = sorted(values)
    idx = (len(sorted_vals) - 1) * p
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_vals[lo]
    weight = idx - lo
    return sorted_vals[lo] * (1 - weight) + sorted_vals[hi] * weight


async def build_snapshot(hours: int, source: str | None, account_id: str | None) -> MetricsSnapshot:
    since = utcnow_naive() - timedelta(hours=hours)

    query = select(MessageLog).where(MessageLog.created_at >= since)
    if source:
        query = query.where(MessageLog.source == source)
    if account_id:
        query = query.where(MessageLog.account_id == account_id)

    async with async_session_maker() as db:
        rows = (await db.execute(query)).scalars().all()

    attempts_total = len(rows)
    attempts_success = sum(1 for r in rows if bool(r.success))
    attempts_failed = attempts_total - attempts_success
    success_rate_percent = round((attempts_success / attempts_total) * 100, 2) if attempts_total else 0.0

    unique_recipients = len({r.recipient for r in rows})
    unique_broadcasts = len({r.source_id for r in rows if r.source_id})

    duration_minutes = max(hours * 60, 1)
    throughput_attempts_per_min = round(attempts_total / duration_minutes, 3)

    latencies_ms: list[float] = []
    for r in rows:
        if r.started_at and r.completed_at:
            delta = (r.completed_at - r.started_at).total_seconds() * 1000
            if delta >= 0:
                latencies_ms.append(delta)

    latency_p50 = percentile(latencies_ms, 0.50)
    latency_p95 = percentile(latencies_ms, 0.95)
    latency_max = max(latencies_ms) if latencies_ms else None

    status_counter = Counter((r.status or "unknown") for r in rows)
    error_counter = Counter((r.error_message or "").strip() for r in rows if r.error_message)

    snapshot = MetricsSnapshot(
        hours=hours,
        source=source,
        account_id=account_id,
        generated_at=utcnow_naive().isoformat(),
        attempts_total=attempts_total,
        attempts_success=attempts_success,
        attempts_failed=attempts_failed,
        success_rate_percent=success_rate_percent,
        unique_recipients=unique_recipients,
        unique_broadcasts=unique_broadcasts,
        throughput_attempts_per_min=throughput_attempts_per_min,
        latency_p50_ms=round(latency_p50, 2) if latency_p50 is not None else None,
        latency_p95_ms=round(latency_p95, 2) if latency_p95 is not None else None,
        latency_max_ms=round(latency_max, 2) if latency_max is not None else None,
        top_statuses=status_counter.most_common(8),
        top_error_messages=error_counter.most_common(8),
    )
    return snapshot


def print_human(snapshot: MetricsSnapshot) -> None:
    print("=== TeleMon Delivery Metrics ===")
    print(f"window_hours: {snapshot.hours}")
    print(f"source: {snapshot.source or 'all'}")
    print(f"account_id: {snapshot.account_id or 'all'}")
    print(f"generated_at: {snapshot.generated_at}")
    print()
    print(f"attempts_total: {snapshot.attempts_total}")
    print(f"attempts_success: {snapshot.attempts_success}")
    print(f"attempts_failed: {snapshot.attempts_failed}")
    print(f"success_rate_percent: {snapshot.success_rate_percent}")
    print(f"unique_recipients: {snapshot.unique_recipients}")
    print(f"unique_broadcasts: {snapshot.unique_broadcasts}")
    print(f"throughput_attempts_per_min: {snapshot.throughput_attempts_per_min}")
    print()
    print("latency_ms:")
    print(f"  p50: {snapshot.latency_p50_ms}")
    print(f"  p95: {snapshot.latency_p95_ms}")
    print(f"  max: {snapshot.latency_max_ms}")
    print()
    print("top_statuses:")
    for status, count in snapshot.top_statuses:
        print(f"  - {status}: {count}")
    print()
    print("top_error_messages:")
    for error, count in snapshot.top_error_messages:
        print(f"  - {count}x {error}")


async def main_async(args: argparse.Namespace) -> None:
    snapshot = await build_snapshot(hours=args.hours, source=args.source, account_id=args.account_id)
    if args.format == "json":
        print(json.dumps(asdict(snapshot), ensure_ascii=False, indent=2))
    else:
        print_human(snapshot)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate delivery performance metrics from message_logs.")
    parser.add_argument("--hours", type=int, default=24, help="Time window in hours (default: 24)")
    parser.add_argument("--source", type=str, default=None, help="Optional source filter (broadcast/reply_macro/auto_reply/scheduled)")
    parser.add_argument("--account-id", type=str, default=None, help="Optional account_id filter")
    parser.add_argument("--format", choices=["human", "json"], default="human", help="Output format")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
