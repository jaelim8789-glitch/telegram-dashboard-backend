"""Regression tests for two production E2E failures:

1. GET /api/delivery-analytics/overview -> 500 in production (Postgres), because
   get_timeline() used SQLite-only func.strftime(). Fixed by branching on the bound
   session's dialect name (app/services/delivery_analytics.py::_timeline_date_expr).
   These tests exercise the real DB session (not a mocked one, unlike the rest of
   test_delivery_analytics.py) so an actual SQL execution error would surface here —
   the pre-existing mocked-session tests could not have caught this class of bug.

2. GET /api/broadcast/recurring -> 404 in production, because GET /{broadcast_id}
   was registered before GET /recurring in app/api/broadcast.py, so Starlette's
   route matching (registration order) let the parameterized route greedily match
   "recurring" as a broadcast_id first. Fixed by reordering the route registrations.
"""

import pytest

from app.services.delivery_analytics import _timeline_date_expr


# ── Root cause 1: strftime/to_char dialect branching ──────────────────


def test_timeline_date_expr_uses_to_char_on_postgres():
    expr = _timeline_date_expr("postgresql", "day")
    compiled = str(expr.compile(compile_kwargs={"literal_binds": True}))
    assert "to_char" in compiled
    assert "strftime" not in compiled


def test_timeline_date_expr_uses_to_char_hour_format_on_postgres():
    expr = _timeline_date_expr("postgresql", "hour")
    compiled = str(expr.compile(compile_kwargs={"literal_binds": True}))
    assert "to_char" in compiled
    assert "HH24" in compiled


def test_timeline_date_expr_uses_strftime_on_sqlite():
    """Preserves the original behavior exactly for SQLite (used in this test suite
    and any SQLite-backed deployment)."""
    expr = _timeline_date_expr("sqlite", "day")
    compiled = str(expr.compile(compile_kwargs={"literal_binds": True}))
    assert "strftime" in compiled
    assert "to_char" not in compiled


@pytest.mark.asyncio
async def test_delivery_analytics_overview_endpoint_succeeds_against_real_db(client, db_session):
    """End-to-end against a real (test) database session -- not a mocked one -- so a
    dialect-specific SQL execution error would actually surface, unlike the fully
    mocked-session tests elsewhere in test_delivery_analytics.py that could not have
    caught this bug class."""
    from app.crud import account as account_crud
    from app.schemas.account import AccountCreate

    account = await account_crud.create_account(db_session, AccountCreate(phone="+821088880001"))

    res = await client.get(f"/api/delivery-analytics/overview?account_id={account.id}&days=7")
    assert res.status_code == 200
    body = res.json()
    assert "summary" in body


@pytest.mark.asyncio
async def test_delivery_analytics_timeline_endpoint_hour_interval_succeeds(client, db_session):
    from app.crud import account as account_crud
    from app.schemas.account import AccountCreate

    account = await account_crud.create_account(db_session, AccountCreate(phone="+821088880002"))

    res = await client.get(f"/api/delivery-analytics/timeline?account_id={account.id}&interval=hour&days=1")
    assert res.status_code == 200


# ── Root cause 2: route registration order ─────────────────────────────


@pytest.mark.asyncio
async def test_recurring_broadcasts_route_not_shadowed_by_broadcast_id_route(client):
    """This is exactly the production failure: before the fix, this returned 404 with
    detail "발송 작업을 찾을 수 없습니다." (from read_broadcast, treating "recurring" as a
    broadcast_id) instead of an empty/populated list from read_recurring_broadcasts."""
    res = await client.get("/api/broadcast/recurring")
    assert res.status_code == 200
    assert isinstance(res.json(), list)


@pytest.mark.asyncio
async def test_get_broadcast_by_id_still_works_after_route_reorder(client, db_session):
    """Guards against a naive fix (e.g. renaming the path) breaking the normal
    GET /{broadcast_id} route instead."""
    res = await client.get("/api/broadcast/does-not-exist-id")
    assert res.status_code == 404
    assert "찾을 수 없습니다" in res.json()["detail"]
