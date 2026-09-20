from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.routers.trade_thesis import ChangeRequest, TradeThesisDependencies, build_trade_thesis_router
from app.trade_thesis import read_repository
from app.trade_thesis.repository import ThesisConflict, capture, stable_hash


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
