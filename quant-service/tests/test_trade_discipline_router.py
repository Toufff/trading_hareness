"""HTTP contract for the read-only discipline routes.

The router is built with fake async dependencies, exactly as the other router
tests here do, so the contract is exercised without a database: what is asserted
is the boundary (status codes, query validation, the research-only envelope),
not a projection's SQL.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.async_trade_discipline_read_repository import valid_uuid
from app.routers.trade_discipline import (
    DEFAULT_STATUSES,
    TradeDisciplineDependencies,
    build_trade_discipline_router,
)

PLAN_ID = "11111111-2222-3333-4444-555555555555"
PLAN_ROW = {"plan_id": PLAN_ID, "plan_key": "citics-primary:600613.SH:2026-09-18:holding",
            "account_key": "citics-primary", "symbol": "600613.SH", "name": "神奇制药",
            "stage": "crash_rebound", "status": "active", "lines": [{"kind": "hard_stop", "price": "7.75"}]}
EVALUATION_ROW = {"evaluation_id": "eval-1", "plan_id": PLAN_ID, "basis": "daily",
                  "plan_state": "exit_signalled", "line_states": []}


def client(*, plans=None, plan=PLAN_ROW, evaluation=EVALUATION_ROW, calls=None):
    async def latest_plans(async_database, account_key, *, statuses, limit):
        if calls is not None:
            calls.append({"database": async_database, "account_key": account_key,
                          "statuses": statuses, "limit": limit})
        return [PLAN_ROW] if plans is None else plans

    async def read_plan(async_database, plan_id):
        if calls is not None:
            calls.append({"database": async_database, "plan_id": plan_id})
        return plan if plan is None or plan_id == plan["plan_id"] else None

    async def latest_evaluation(async_database, plan_id, *, basis):
        if calls is not None:
            calls.append({"database": async_database, "plan_id": plan_id, "basis": basis})
        return evaluation

    app = FastAPI()
    app.include_router(build_trade_discipline_router(TradeDisciplineDependencies(
        async_database="async-db", latest_plans=latest_plans, read_plan=read_plan,
        latest_evaluation=latest_evaluation)))
    return TestClient(app)


def test_latest_plans_returns_the_research_only_envelope():
    calls = []
    response = client(calls=calls).get("/api/v1/discipline/plans/latest?account_key=citics-primary")
    assert response.status_code == 200
    payload = response.json()
    assert payload["account_key"] == "citics-primary"
    assert payload["count"] == 1
    assert payload["items"][0]["symbol"] == "600613.SH"
    assert payload["live_orders"] is False
    assert payload["boundary"] == "research_only_human_decision_support"
    assert calls[0]["database"] == "async-db"
    assert calls[0]["statuses"] == DEFAULT_STATUSES
    assert calls[0]["limit"] == 50


def test_latest_plans_requires_an_account_key():
    assert client().get("/api/v1/discipline/plans/latest").status_code == 422


def test_latest_plans_passes_a_validated_status_filter_through():
    calls = []
    response = client(calls=calls).get(
        "/api/v1/discipline/plans/latest?account_key=citics-primary&status=active,expired&limit=5")
    assert response.status_code == 200
    assert calls[0]["statuses"] == ("active", "expired")
    assert calls[0]["limit"] == 5
    assert response.json()["statuses"] == ["active", "expired"]


def test_an_unknown_status_is_rejected_instead_of_silently_widening_the_read():
    response = client().get("/api/v1/discipline/plans/latest?account_key=citics-primary&status=whatever")
    assert response.status_code == 422
    assert "whatever" in response.json()["detail"]


def test_the_limit_is_bounded():
    assert client().get(
        "/api/v1/discipline/plans/latest?account_key=citics-primary&limit=0").status_code == 422
    assert client().get(
        "/api/v1/discipline/plans/latest?account_key=citics-primary&limit=5000").status_code == 422


def test_an_empty_account_reads_as_an_empty_list_not_a_404():
    response = client(plans=[]).get("/api/v1/discipline/plans/latest?account_key=nobody")
    assert response.status_code == 200
    assert response.json() == {"account_key": "nobody", "statuses": list(DEFAULT_STATUSES), "limit": 50,
                               "count": 0, "items": [], "live_orders": False,
                               "boundary": "research_only_human_decision_support"}


def test_one_plan_is_read_by_id():
    response = client().get(f"/api/v1/discipline/plans/{PLAN_ID}")
    assert response.status_code == 200
    assert response.json()["plan"]["plan_key"] == PLAN_ROW["plan_key"]
    assert response.json()["live_orders"] is False


def test_an_unknown_plan_id_is_a_404():
    assert client(plan=None).get(f"/api/v1/discipline/plans/{PLAN_ID}").status_code == 404
    assert client().get("/api/v1/discipline/plans/not-a-uuid").status_code == 404


def test_the_latest_evaluation_is_read_for_one_plan():
    calls = []
    response = client(calls=calls).get(
        f"/api/v1/discipline/evaluations/latest?plan_id={PLAN_ID}&basis=daily")
    assert response.status_code == 200
    assert response.json()["evaluation"]["plan_state"] == "exit_signalled"
    assert calls[0]["basis"] == "daily"


def test_an_unevaluated_plan_is_a_404_and_a_bad_basis_is_a_422():
    assert client(evaluation=None).get(
        f"/api/v1/discipline/evaluations/latest?plan_id={PLAN_ID}").status_code == 404
    assert client().get(
        f"/api/v1/discipline/evaluations/latest?plan_id={PLAN_ID}&basis=hourly").status_code == 422


def test_the_router_exposes_exactly_the_three_documented_read_routes():
    app = client().app
    routes = {(tuple(sorted(route.methods)), route.path) for route in app.routes
              if getattr(route, "path", "").startswith("/api/v1/discipline")}
    assert routes == {
        (("GET",), "/api/v1/discipline/plans/latest"),
        (("GET",), "/api/v1/discipline/plans/{plan_id}"),
        (("GET",), "/api/v1/discipline/evaluations/latest"),
    }


def test_only_a_real_uuid_reaches_the_plan_projection():
    assert valid_uuid(PLAN_ID) == PLAN_ID
    assert valid_uuid("not-a-uuid") is None
    assert valid_uuid(None) is None
