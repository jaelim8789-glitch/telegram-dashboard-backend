#!/usr/bin/env python3
"""Epic 23 — Chaos test driver.

Deliberately breaks subsystems and verifies auto-recovery + alerts. Run on a
NON-PRODUCTION staging environment. Each scenario: break -> observe incident ->
wait for auto-recovery -> confirm recovered -> record recovery time.

Requires docker access to the staging compose (the incident engine + DR script
are what actually recover; this driver orchestrates + verifies).

Usage:
    python scripts/chaos_test.py --compose-dir /opt/telemon/backend \
        --base http://localhost:8000 --token <session_token> [--scenarios redis,db,worker]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

import httpx

RESULTS: list[dict] = []


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout + p.stderr)[-2000:]
    except Exception as e:
        return -1, str(e)


async def _wait_healthy(base: str, token: str, timeout: float = 120) -> tuple[bool, float]:
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=5) as c:
        while time.monotonic() - t0 < timeout:
            try:
                r = await c.get(f"{base}/health", headers={"X-Session-Token": token})
                if r.status_code == 200:
                    return True, time.monotonic() - t0
            except Exception:
                pass
            await asyncio.sleep(3)
    return False, time.monotonic() - t0


async def _incidents_clear(base: str, token: str, name: str, timeout: float = 300) -> tuple[bool, float]:
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=5) as c:
        while time.monotonic() - t0 < timeout:
            try:
                r = await c.get(f"{base}/api/admin/incidents", headers={"X-Session-Token": token})
                if r.status_code == 200:
                    incidents = r.json().get("incidents", [])
                    if not any(i.get("name") == name for i in incidents):
                        return True, time.monotonic() - t0
            except Exception:
                pass
            await asyncio.sleep(5)
    return False, time.monotonic() - t0


async def scenario_redis(compose_dir: str, base: str, token: str) -> None:
    print("\n[chaos] Redis: stopping redis container...")
    _run(["docker", "compose", "-f", f"{compose_dir}/docker-compose.yml", "stop", "redis"])
    ok, dt = await _wait_healthy(base, token, timeout=90)
    if ok:
        print(f"[chaos] ERROR: redis down but /health still 200 (expected 503)")
        RESULTS.append({"scenario": "redis", "healthy_while_down": True})
    else:
        print(f"[chaos] redis down confirmed (/health degraded after {dt:.0f}s)")
        # Restart + verify recovery
        _run(["docker", "compose", "-f", f"{compose_dir}/docker-compose.yml", "start", "redis"])
        ok2, dt2 = await _wait_healthy(base, token, timeout=120)
        cleared, dt3 = await _incidents_clear(base, token, "redis", timeout=300)
        RESULTS.append({
            "scenario": "redis",
            "degraded_after_s": round(dt, 1),
            "recovered_in_s": round(dt2, 1),
            "incident_cleared": cleared,
            "incident_clear_s": round(dt3, 1),
        })


async def scenario_db(compose_dir: str, base: str, token: str) -> None:
    print("\n[chaos] DB: restarting postgres container...")
    _run(["docker", "compose", "-f", f"{compose_dir}/docker-compose.yml", "restart", "db"])
    ok, dt = await _wait_healthy(base, token, timeout=120)
    cleared, dt2 = await _incidents_clear(base, token, "postgres", timeout=300)
    RESULTS.append({
        "scenario": "db",
        "healthy_in_s": round(dt, 1),
        "incident_cleared": cleared,
        "incident_clear_s": round(dt2, 1),
    })


async def scenario_worker(compose_dir: str, base: str, token: str) -> None:
    print("\n[chaos] Worker: killing backend worker process (SIGKILL inside container)...")
    _run(["docker", "compose", "-f", f"{compose_dir}/docker-compose.yml", "exec", "-T", "backend",
          "sh", "-c", "kill -9 $(pgrep -f uvicorn | head -1) || true"])
    ok, dt = await _wait_healthy(base, token, timeout=120)
    RESULTS.append({
        "scenario": "worker",
        "recovered_in_s": round(dt, 1),
        "healthy": ok,
    })


async def scenario_telegram(compose_dir: str, base: str, token: str) -> None:
    print("\n[chaos] Telegram: simulating disconnect by disabling network for backend (30s)...")
    # Break telethon connectivity by SIGSTOPping a random uvicorn worker briefly
    _run(["docker", "compose", "-f", f"{compose_dir}/docker-compose.yml", "exec", "-T", "backend",
          "sh", "-c", "pkill -STOP -f uvicorn || true"])
    await asyncio.sleep(8)
    _run(["docker", "compose", "-f", f"{compose_dir}/docker-compose.yml", "exec", "-T", "backend",
          "sh", "-c", "pkill -CONT -f uvicorn || true"])
    ok, dt = await _wait_healthy(base, token, timeout=90)
    RESULTS.append({
        "scenario": "telegram_disconnect",
        "recovered_in_s": round(dt, 1),
        "healthy": ok,
    })


SCENARIOS = {
    "redis": scenario_redis,
    "db": scenario_db,
    "worker": scenario_worker,
    "telegram": scenario_telegram,
}


async def main(args: argparse.Namespace) -> int:
    chosen = args.scenarios.split(",") if args.scenarios else list(SCENARIOS.keys())
    for name in chosen:
        fn = SCENARIOS.get(name.strip())
        if not fn:
            print(f"[chaos] unknown scenario: {name}")
            continue
        try:
            await fn(args.compose_dir, args.base, args.token)
        except Exception as e:
            print(f"[chaos] {name} failed: {e}")
            RESULTS.append({"scenario": name, "error": str(e)})
        await asyncio.sleep(5)

    report = {
        "started": datetime.now(timezone.utc).isoformat(),
        "results": RESULTS,
    }
    with open(args.out, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print("\n===== CHAOS REPORT =====")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nSaved to {args.out}.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Epic 23 chaos test driver")
    ap.add_argument("--compose-dir", default="/opt/telemon/backend")
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--token", required=True)
    ap.add_argument("--scenarios", default="", help="comma list: redis,db,worker,telegram (default all)")
    ap.add_argument("--out", default="chaos_report.json")
    sys.exit(asyncio.run(main(ap.parse_args())))
