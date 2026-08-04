"""Incident Engine tests.

Verifies the Epic 20 contract with a fake Redis:
  - incident open on critical check + alert fired
  - incident resolve moves to history with downtime
  - health_score returns 0-100 composite
  - exponential retry loop attempts recovery
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.incident_engine import IncidentEngine, RETRY_BACKOFF


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttls[key] = ex
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)
        return 1

    async def exists(self, key):
        return key in self.store

    async def keys(self, pattern):
        prefix = pattern.replace("*", "")
        return [k for k in self.store if k.startswith(prefix)]


@pytest.fixture
def engine(monkeypatch):
    e = IncidentEngine()
    monkeypatch.setattr(e, "_redis", AsyncMock(return_value=_FakeRedis()))
    return e


@pytest.mark.asyncio
async def test_open_incident_writes_redis_and_alerts(engine, monkeypatch):
    alerts = []
    monkeypatch.setattr("app.services.incident_engine.send_alert", lambda *a, **k: alerts.append((a, k)))
    r = await engine._redis()

    await engine._open_incident("redis", "critical", "Redis 연결 실패")

    assert f"incident:active:redis" in r.store
    import json
    inc = json.loads(r.store["incident:active:redis"])
    assert inc["severity"] == "critical"
    assert inc["state"] == "critical"
    assert len(alerts) == 1
    assert "CRITICAL" in alerts[0][0][0]


@pytest.mark.asyncio
async def test_resolve_moves_to_history(engine, monkeypatch):
    await engine._open_incident("postgres", "critical", "PG down")
    r = await engine._redis()

    await engine._resolve_incident("postgres", downtime_seconds=42)

    assert "incident:active:postgres" not in r.store
    history = json.loads(r.store["incident:history"])
    assert len(history) == 1
    assert history[0]["name"] == "postgres"
    assert history[0]["downtime_seconds"] == 42.0
    assert history[0]["status"] == "Recovered"


@pytest.mark.asyncio
async def test_health_score(engine):
    # With a healthy fake redis + real DB probe unavailable in tests, db check
    # will fail -> score reflects degraded state; just assert shape + range.
    score = await engine.health_score()
    assert "average" in score
    assert "components" in score
    assert 0 <= score["average"] <= 100
    assert set(score["components"].keys()) == {"database", "redis", "scheduler", "telegram", "worker"}


@pytest.mark.asyncio
async def test_recovery_retry_schedule(engine):
    assert RETRY_BACKOFF == [5, 15, 30]
    # _recover_loop should attempt recovery and resolve on success
    calls = []
    async def fake_attempt(name):
        calls.append(name)
        return True  # succeed on first try
    monkeypatch = patch.object(engine, "_attempt_recovery", new=fake_attempt)
    with monkeypatch:
        await engine._recover_loop("redis")
    assert calls == ["redis"]
