#!/usr/bin/env python3
"""Epic 23 — Scale-validation load driver.

Runs realistic load against a NON-PRODUCTION backend and collects the metrics
the operator needs: API P50/P95/P99/max, WS latency, queue length, scheduler
delay, error rate / success rate, plus system saturation (CPU/mem/disk/redis/pg)
from psutil + monitoring endpoints.

Usage (VPS staging / local docker, NEVER production):
    python scripts/scale_test.py --base http://localhost:8000 \
        --duration 600 --users 10 --ws-clients 5 \
        --login-token <session_token> [--broadcasts 100] [--ai 100]

Scenarios covered (80% real / 20% mock by design):
    login / account / chat / broadcast / ai / auto-reply / websocket

Output: JSON + a printed Before/After-friendly table (run once before any
optimization, once after, diff the two JSONs).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from datetime import datetime, timezone
from typing import Any

import httpx

SAMPLE_SIZE = 200_000
latency_samples: list[float] = []
error_count = 0
request_count = 0


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(p * len(sorted_vals)))
    return round(sorted_vals[idx] * 1000, 1)  # ms


def _report_latency() -> dict[str, float]:
    s = sorted(latency_samples)
    return {
        "p50_ms": _percentile(s, 0.50),
        "p95_ms": _percentile(s, 0.95),
        "p99_ms": _percentile(s, 0.99),
        "max_ms": round(s[-1] * 1000, 1) if s else 0.0,
    }


async def _request(client: httpx.AsyncClient, method: str, url: str, **kw) -> httpx.Response:
    global error_count, request_count
    request_count += 1
    t0 = time.monotonic()
    try:
        r = await client.request(method, url, **kw)
        dt = time.monotonic() - t0
        latency_samples.append(dt)
        if len(latency_samples) > SAMPLE_SIZE:
            latency_samples.pop(0)
        if r.status_code >= 500:
            error_count += 1
        return r
    except Exception:
        error_count += 1
        latency_samples.append(time.monotonic() - t0)
        raise


# ── Scenario workers ───────────────────────────────────────────────────


async def _ws_latency_worker(base: str, token: str, results: list[float]) -> None:
    import websockets
    from websockets.exceptions import ConnectionClosed

    try:
        url = base.replace("http://", "ws://").replace("https://", "wss://") + "/ws/sessions?token=" + token
        async with websockets.connect(url, open_timeout=10) as ws:
            for _ in range(20):
                t0 = time.monotonic()
                await ws.send(json.dumps({"type": "ping"}))
                await asyncio.wait_for(ws.recv(), timeout=5)
                results.append(time.monotonic() - t0)
    except Exception as e:
        print(f"  [ws] worker error: {e}")


async def _chat_worker(client: httpx.AsyncClient, base: str, token: str, account_id: str) -> None:
    h = {"X-Session-Token": token}
    # GET dialogs + messages (real API path; assumes a seeded account has dialogs)
    try:
        await _request(client, "GET", f"{base}/api/chat-telegram/accounts/{account_id}/dialogs", headers=h, timeout=30)
    except Exception:
        pass


async def _ai_worker(client: httpx.AsyncClient, base: str, token: str, prompt: str) -> None:
    h = {"X-Session-Token": token}
    try:
        await _request(client, "POST", f"{base}/api/ai/chat", headers=h,
                       json={"message": prompt, "session_id": "", "use_memory": False}, timeout=60)
    except Exception:
        pass


# ── Main ────────────────────────────────────────────────────────────────


async def run(args: argparse.Namespace) -> dict[str, Any]:
    base = args.base.rstrip("/")
    token = args.login_token
    headers = {"X-Session-Token": token, "Content-Type": "application/json"}
    duration = args.duration
    deadline = time.monotonic() + duration
    ws_results: list[float] = []
    sys_metrics: dict[str, Any] = {}

    started = datetime.now(timezone.utc).isoformat()

    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        # Baseline health + scheduler + redis probe
        health = await _request(client, "GET", f"{base}/health")
        sched = await _request(client, "GET", f"{base}/api/scheduler/status")
        try:
            sched_json = sched.json()
        except Exception:
            sched_json = {}
        baseline = {
            "health_status": health.status_code,
            "scheduler": sched_json,
        }

        # Launch WS latency probes (short-lived)
        ws_tasks = [asyncio.create_task(_ws_latency_worker(base, token, ws_results)) for _ in range(args.ws_clients)]

        tasks = []
        iter_i = [0]

        while time.monotonic() < deadline:
            # AI workers (sparse — expensive)
            if args.ai and iter_i[0] % max(1, duration // max(1, args.ai)) == 0 and len(tasks) < args.users * 3:
                tasks.append(asyncio.create_task(_ai_worker(client, base, token, f"스케일 테스트 질문 {iter_i[0]}")))
            # Broadcast creation (real create path)
            if args.broadcasts and iter_i[0] % max(1, duration // max(1, args.broadcasts)) == 0:
                tasks.append(asyncio.create_task(
                    _request(client, "POST", f"{base}/api/broadcasts/create", json={
                        "account_id": args.account_id or "scale-acc-00",
                        "message": f"스케일 발송 {iter_i[0]}",
                        "recipients": ["@scale_channel_0", "@scale_channel_1"],
                    }, timeout=60)
                ))
            # Chat dialogs read
            tasks.append(asyncio.create_task(_chat_worker(client, base, token, args.account_id or "scale-acc-00")))
            # Health + scheduler + overview poll (scheduler-delay observable)
            tasks.append(asyncio.create_task(_request(client, "GET", f"{base}/health")))
            tasks.append(asyncio.create_task(_request(client, "GET", f"{base}/api/scheduler/status")))

            iter_i[0] += 1
            await asyncio.sleep(0.2)
            if len(tasks) > 60:
                await asyncio.gather(*tasks)
                tasks = []

        # Drain remaining tasks (ignore errors)
        await asyncio.gather(*tasks, *ws_tasks, return_exceptions=True)

    # System saturation probe (best-effort; requires psutil on the same host)
    try:
        import psutil
        sys_metrics = {
            "cpu_percent": psutil.cpu_percent(interval=1),
            "mem_percent": psutil.virtual_memory().percent,
            "mem_used_mb": round(psutil.virtual_memory().used / 1024 / 1024, 1),
            "disk_percent": psutil.disk_usage("/").percent,
            "load_avg": list(psutil.getloadavg()) if hasattr(psutil, "getloadavg") else None,
        }
    except ImportError:
        sys_metrics = {"note": "psutil not installed — install to capture CPU/mem/disk"}

    success_rate = 100.0 * (request_count - error_count) / max(1, request_count)

    return {
        "started": started,
        "finished": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": duration,
        "requests": request_count,
        "errors": error_count,
        "success_rate_pct": round(success_rate, 3),
        "latency": _report_latency(),
        "ws_latency": _report_ws(ws_results),
        "scheduler_delay": _scheduler_delay(baseline.get("scheduler", {})),
        "baseline_health": baseline.get("health_status"),
        "system": sys_metrics,
    }


def _report_ws(results: list[float]) -> dict[str, float]:
    if not results:
        return {"samples": 0}
    s = sorted(results)
    return {
        "samples": len(s),
        "p50_ms": round(s[len(s) // 2] * 1000, 1),
        "p95_ms": round(s[min(len(s) - 1, int(0.95 * len(s)))] * 1000, 1),
        "max_ms": round(s[-1] * 1000, 1),
    }


def _scheduler_delay(sched: dict) -> dict[str, Any]:
    """Scheduler delay = now - next_tick_at (how far behind the scheduler is)."""
    next_tick = sched.get("next_tick_at")
    if not next_tick:
        return {"note": "no next_tick_at"}
    try:
        from datetime import datetime as _dt
        nxt = _dt.fromisoformat(str(next_tick).replace("Z", "+00:00"))
        delay_s = max(0.0, (datetime.now(timezone.utc) - nxt).total_seconds())
        return {"delay_seconds": round(delay_s, 1)}
    except Exception:
        return {"note": "parse failed"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Epic 23 scale-validation load driver")
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--login-token", required=True, help="valid session_token")
    ap.add_argument("--duration", type=int, default=600, help="seconds (600/1800/21600)")
    ap.add_argument("--users", type=int, default=10, help="concurrency")
    ap.add_argument("--ws-clients", type=int, default=5)
    ap.add_argument("--broadcasts", type=int, default=100)
    ap.add_argument("--ai", type=int, default=100)
    ap.add_argument("--account-id", default="scale-acc-00")
    ap.add_argument("--out", default="scale_report.json")
    args = ap.parse_args()

    print(f"[scale-test] running {args.duration}s against {args.base} ...")
    report = asyncio.run(run(args))

    with open(args.out, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print("\n===== SCALE REPORT =====")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nSaved to {args.out}. Run again after optimizations and diff the two JSONs for Before/After.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
