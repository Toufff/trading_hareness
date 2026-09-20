from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.routers.trade_thesis import ChangeRequest, TradeThesisDependencies, build_trade_thesis_router
from app.trade_thesis import read_repository
from app.trade_thesis.repository import (
    ThesisConflict,
    active_thesis,
    capture,
    latest_previous_evaluation,
    stable_hash,
)
from app.trade_thesis import bindings


class Result:
    def __init__(self, rows): self.rows = rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows


class CaptureConnection:
    def __init__(self, frozen=None): self.frozen = frozen; self.calls = []
    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        if "event_type='capture'" in sql:
            return Result([] if self.frozen is None else [self.frozen])
        if "INSERT INTO quant.trade_thesis_revisions" in sql:
            return Result([{"event_id": "e1", "event_type": "capture", "content_revision": 1,
                            "content_hash": "a" * 64, "created_at": "now"}])
        return Result([])


def test_capture_rejects_payload_drift_instead_of_resetting_deadline():
    original = {"thesis_id": "HT-A", "revision": 1, "symbol": "600185.SH",
                "source_run_id": "old", "terminal_deadline": "2026-09-20T07:00:00Z"}
    connection = CaptureConnection({"event_id": "e1", "content_hash": stable_hash(original),
                                    "created_at": "then", "payload": original})
    retry = {**original, "source_run_id": "new", "terminal_deadline": "2026-10-20T07:00:00Z"}
    with pytest.raises(ThesisConflict):
        capture(connection, retry)
    assert not any("INSERT INTO" in sql for sql, _ in connection.calls)


class AsyncResult:
    def __init__(self, rows): self.rows = rows
    async def fetchall(self): return self.rows


class AsyncConnection:
    def __init__(self, responses): self.responses = list(responses); self.calls = []
    async def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        return AsyncResult(self.responses.pop(0))


class AsyncDB:
    def __init__(self, *responses): self.connection = AsyncConnection(responses)
    @asynccontextmanager
    async def transaction(self): yield self.connection


def test_async_timeline_is_select_only_and_keeps_rejected_events():
    database = AsyncDB([{"event_type": "reject"}], [{"namespace": "shadow"}])
    result = asyncio.run(read_repository.thesis_timeline(database, "HT-A"))
    assert result == {"thesis_id": "HT-A", "revisions": [{"event_type": "reject"}],
                      "evaluations": [{"namespace": "shadow"}]}
    assert all(sql.startswith("SELECT") for sql, _ in database.connection.calls)


def test_previous_evaluation_and_historical_review_conflicts_are_pit_visible_only():
    class Connection:
        def __init__(self): self.calls = []
        def execute(self, sql, params):
            self.calls.append((" ".join(sql.split()), params))
            return Result([])

    connection = Connection()
    cutoff = "2026-09-20T07:00:00Z"
    assert latest_previous_evaluation(connection, "HT-A", cutoff) is None
    previous_sql, previous_params = connection.calls[-1]
    assert "cutoff_at<%s AND created_at<=%s" in previous_sql
    assert previous_params[-2:] == (cutoff, cutoff)

    assert active_thesis(connection, "HT-A", as_of=cutoff) is None
    active_sql, active_params = connection.calls[-1]
    assert "conflict.created_at<=%s" in active_sql
    assert active_params == ("HT-A", cutoff, cutoff, cutoff, cutoff)


def test_async_list_hides_only_reviews_visible_by_as_of():
    database = AsyncDB([])
    asyncio.run(read_repository.list_theses(
        database, as_of=datetime.fromisoformat("2026-09-20T07:00:00+00:00"),
    ))
    sql, params = database.connection.calls[0]
    assert "conflict.created_at<=%s" in sql
    assert len(params) == 11


def test_router_maps_stale_expected_revision_to_409():
    class DB:
        class Tx:
            def __enter__(self): return object()
            def __exit__(self, *_): return False
        def transaction(self): return self.Tx()

    async def run_database(call, *args, **kwargs): return call(*args)
    async def reads(*args, **kwargs): return []
    async def timeline(*args, **kwargs): return None
    def conflict(*args, **kwargs): raise ThesisConflict("expected_revision is stale")
    router = build_trade_thesis_router(TradeThesisDependencies(
        database=DB(), async_database=object(), run_database=run_database,
        list_theses=reads, timeline=timeline, propose_change=conflict,
        review_change=conflict, evaluate_source_run=lambda *_a, **_k: {},
    ))
    endpoint = next(route.endpoint for route in router.routes if route.path.endswith("/{thesis_id}/changes"))
    with pytest.raises(HTTPException) as raised:
        asyncio.run(endpoint("HT-A", ChangeRequest(expected_revision=1, changes={"claim": "new"}, actor="agent")))
    assert raised.value.status_code == 409


def test_evaluate_http_model_has_no_raw_evidence_field():
    from app.routers.trade_thesis import EvaluateRequest
    with pytest.raises(ValueError):
        EvaluateRequest.model_validate({"source_run_id": "run-1", "evidence": [{"fabricated": True}]})


def test_evaluate_route_uses_real_database_executor_signature():
    from app.runtime_executors import run_database_blocking

    captured = {}

    def evaluate_source_run(database, source_run_id, *, cutoff_at, symbols, namespace):
        captured.update(database=database, source_run_id=source_run_id, cutoff_at=cutoff_at,
                        symbols=symbols, namespace=namespace)
        return {"status": "completed", "evaluated": 1}

    async def reads(*_args, **_kwargs): return []
    async def timeline(*_args, **_kwargs): return None
    database = object()
    app = FastAPI()
    app.include_router(build_trade_thesis_router(TradeThesisDependencies(
        database=database, async_database=object(), run_database=run_database_blocking,
        list_theses=reads, timeline=timeline, propose_change=lambda *_a, **_k: {},
        review_change=lambda *_a, **_k: {}, evaluate_source_run=evaluate_source_run,
    )))

    with TestClient(app) as client:
        response = client.post("/api/v1/research/theses/evaluate", json={
            "source_run_id": "run-42", "cutoff_at": "2026-09-20T07:00:00Z",
            "symbols": ["600000.SH"], "namespace": "shadow",
        })

    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "completed", "evaluated": 1, "decision_binding": False, "live_effect": "none",
    }
    assert captured["database"] is database and captured["source_run_id"] == "run-42"
    assert captured["symbols"] == ["600000.SH"] and captured["namespace"] == "shadow"
    assert captured["cutoff_at"].isoformat() == "2026-09-20T07:00:00+00:00"


def test_binding_requires_matching_active_holding_plan(monkeypatch):
    thesis = {"thesis_id": "HT-A", "revision": 2, "symbol": "600000.SH"}
    plan = {
        "plan_id": "00000000-0000-0000-0000-000000000001", "account_key": "acct-a", "symbol": "600000.SH",
        "plan_kind": "holding", "status": "active", "valid_until": "2026-09-22T08:00:00Z",
        "as_of_at": "2026-09-19T08:00:00Z", "created_at": "2026-09-19T08:00:00Z",
        "quality": [], "position": {"snapshot_id": "snap-1", "quantity": 100},
    }
    saved = {}
    monkeypatch.setattr(bindings, "active_thesis", lambda *_a, **_k: thesis)
    monkeypatch.setattr(bindings, "read_plan", lambda *_a, **_k: plan)
    monkeypatch.setattr(bindings, "latest_binding", lambda *_a, **_k: None)
    monkeypatch.setattr(bindings, "_has_active_successor", lambda *_a, **_k: False)
    monkeypatch.setattr(bindings, "persist_binding", lambda _c, value: saved.update(value) or {"status": "created"})
    result = bindings.create_binding(
        object(), "HT-A", thesis_revision=2, account_key="acct-a", symbol="600000.SH",
        position_episode_id="holding-episode-1", plan_id="00000000-0000-0000-0000-000000000001",
        bound_at="2026-09-20T08:00:00Z", evidence_refs=["position_snapshot:snap-1"],
    )
    assert result["status"] == "created" and saved["position_episode_id"] == "holding-episode-1"

    with pytest.raises(bindings.BindingValidationError, match="account"):
        bindings.create_binding(
            object(), "HT-A", thesis_revision=2, account_key="acct-b", symbol="600000.SH",
            position_episode_id="holding-episode-1", plan_id="00000000-0000-0000-0000-000000000001",
            bound_at="2026-09-20T08:00:00Z", evidence_refs=["position_snapshot:snap-1"],
        )
    with pytest.raises(bindings.BindingValidationError, match="active"):
        bindings.create_binding(
            object(), "HT-A", thesis_revision=2, account_key="acct-a", symbol="600000.SH",
            position_episode_id="holding-episode-1", plan_id="00000000-0000-0000-0000-000000000001",
            bound_at="2026-09-23T08:00:00Z", evidence_refs=["position_snapshot:snap-1"],
        )


def test_load_bound_plan_exposes_only_persisted_holding_and_hard_risk(monkeypatch):
    binding = {"binding_id": "b1", "thesis_revision": 2, "plan_id": "00000000-0000-0000-0000-000000000001"}
    plan = {"plan_id": "00000000-0000-0000-0000-000000000001", "plan_kind": "holding", "status": "active",
            "as_of_at": "2026-09-19T08:00:00Z", "created_at": "2026-09-19T08:00:00Z",
            "quality": [], "valid_until": "2026-09-22T08:00:00Z",
            "position": {"snapshot_id": "snap-1", "quantity": 100}}
    evaluation = {"plan_state": "exit_signalled", "basis": "daily",
                  "trading_date": date(2026, 9, 20), "line_states": [
        {"kind": "hard_stop", "state": "triggered", "trigger_price": "7.55"},
    ]}

    class Connection:
        def execute(self, sql, *_args):
            if "discipline_evaluations" in sql:
                assert "created_at<=%s" in sql
                return Result([evaluation])
            if "market_trade_calendar" in sql:
                return Result([{"trading_date": date(2026, 9, 20)}])
            return Result([])

    monkeypatch.setattr(bindings, "latest_binding", lambda *_a, **_k: dict(binding))
    monkeypatch.setattr(bindings, "read_plan", lambda *_a, **_k: dict(plan))
    result = bindings.load_bound_plan(Connection(), "HT-A", account_key="acct-a",
                                      as_of="2026-09-20T08:00:00Z")
    assert result["status"] == "bound" and result["holding"]["quantity"] == 100
    assert result["binding"]["revision"] == 2 and result["risk"]["hard_risk"] is True

    stale = bindings.load_bound_plan(Connection(), "HT-A", account_key="acct-a",
                                     as_of="2026-09-23T08:00:00Z")
    assert stale["status"] == "stale" and stale["stale_reason"] == "plan_expired"
    assert stale["holding"] is None and stale["risk"]["actionable"] is False


def test_evaluation_created_after_cutoff_is_invisible_and_missing_is_stale(monkeypatch):
    monkeypatch.setattr(bindings, "latest_binding", lambda *_a, **_k: {
        "binding_id": "b1", "thesis_revision": 1,
        "plan_id": "00000000-0000-0000-0000-000000000001",
    })
    monkeypatch.setattr(bindings, "read_plan", lambda *_a, **_k: {
        "plan_id": "00000000-0000-0000-0000-000000000001", "plan_kind": "holding",
        "status": "active", "as_of_at": "2026-09-19T08:00:00Z",
        "created_at": "2026-09-19T08:00:00Z", "valid_until": "2026-09-22T08:00:00Z",
        "quality": [], "position": {"snapshot_id": "snap-1", "quantity": 100},
    })

    class Connection:
        def execute(self, sql, params):
            if "discipline_evaluations" in sql:
                assert "created_at<=%s" in sql and params[1] == params[2]
                return Result([])  # a later-created row is PIT-invisible
            return Result([])

    result = bindings.load_bound_plan(Connection(), "HT-A", account_key="acct-a",
                                      as_of="2026-09-20T08:00:00Z")
    assert result["status"] == "stale" and result["stale_reason"] == "evaluation_missing"
    assert result["holding"] is None and result["risk"]["actionable"] is False


def test_new_buy_binding_is_explicit_and_never_fabricates_a_holding(monkeypatch):
    plan = {"plan_id": "00000000-0000-0000-0000-000000000002", "account_key": "acct-a", "symbol": "600000.SH",
            "plan_kind": "new_buy", "status": "active", "valid_until": "2026-09-22T08:00:00Z",
            "as_of_at": "2026-09-19T08:00:00Z", "created_at": "2026-09-19T08:00:00Z",
            "quality": [], "position": None}
    monkeypatch.setattr(bindings, "active_thesis", lambda *_a, **_k: {
        "thesis_id": "HT-A", "revision": 1, "symbol": "600000.SH",
    })
    monkeypatch.setattr(bindings, "read_plan", lambda *_a, **_k: plan)
    monkeypatch.setattr(bindings, "latest_binding", lambda *_a, **_k: None)
    monkeypatch.setattr(bindings, "_has_active_successor", lambda *_a, **_k: False)
    monkeypatch.setattr(bindings, "persist_binding", lambda *_a, **_k: {"status": "created"})
    with pytest.raises(bindings.BindingValidationError, match="prospective"):
        bindings.create_binding(
            object(), "HT-A", thesis_revision=1, account_key="acct-a", symbol="600000.SH",
            position_episode_id="holding-1", plan_id="00000000-0000-0000-0000-000000000002", bound_at="2026-09-20T08:00:00Z",
        )
    result = bindings.create_binding(
        object(), "HT-A", thesis_revision=1, account_key="acct-a", symbol="600000.SH",
        position_episode_id="prospective:plan:00000000-0000-0000-0000-000000000002", plan_id="00000000-0000-0000-0000-000000000002",
        binding_source="manual_new_buy", bound_at="2026-09-20T08:00:00Z",
    )
    assert result["binding"]["position_episode_id"] == "prospective:plan:00000000-0000-0000-0000-000000000002"


def test_accountless_load_refuses_to_choose_between_accounts():
    class Connection:
        def execute(self, *_args): return Result([{"account_key": "a"}, {"account_key": "b"}])

    result = bindings.load_bound_plan(Connection(), "HT-A", symbol="600000.SH",
                                      as_of="2026-09-20T08:00:00Z")
    assert result["status"] == "ambiguous_account"
    assert result["holding"] is None and result["risk"]["actionable"] is False


def test_binding_routes_use_typed_contract_and_database_executor():
    from app.runtime_executors import run_database_blocking

    class DB:
        class Tx:
            def __enter__(self): return "connection"
            def __exit__(self, *_args): return False
        def transaction(self): return self.Tx()

    captured = {}

    def create(connection, thesis_id, **kwargs):
        captured.update(connection=connection, thesis_id=thesis_id, **kwargs)
        return {"status": "created", "binding_id": "b1"}

    def load(_database, thesis_id, **kwargs):
        return {"status": "bound", "thesis_id": thesis_id, "plan_kind": "holding",
                "holding": {"quantity": 100}, "risk": {"hard_risk": False}, **kwargs}

    async def reads(*_args, **_kwargs): return []
    async def timeline(*_args, **_kwargs): return None
    app = FastAPI()
    app.include_router(build_trade_thesis_router(TradeThesisDependencies(
        database=DB(), async_database=object(), run_database=run_database_blocking,
        list_theses=reads, timeline=timeline, propose_change=lambda *_a, **_k: {},
        review_change=lambda *_a, **_k: {}, evaluate_source_run=lambda *_a, **_k: {},
        create_binding=create, load_bound_plan=load,
    )))
    with TestClient(app) as client:
        posted = client.post("/api/v1/research/theses/HT-A/bindings", json={
            "thesis_revision": 1, "account_key": "acct-a", "symbol": "600000.SH",
            "position_episode_id": "episode-1", "plan_id": "00000000-0000-0000-0000-000000000001",
            "evidence_refs": ["position_snapshot:snap-1"],
        })
        loaded = client.get("/api/v1/research/theses/HT-A/binding", params={
            "account_key": "acct-a", "symbol": "600000.SH",
            "as_of": "2026-09-20T08:00:00Z",
        })
    assert posted.status_code == 200 and posted.json()["decision_binding"] is False
    assert captured["connection"] == "connection" and captured["account_key"] == "acct-a"
    assert loaded.status_code == 200 and loaded.json()["holding"]["quantity"] == 100
