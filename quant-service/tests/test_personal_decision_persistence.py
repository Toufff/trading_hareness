"""Persistence and HTTP boundary coverage for personal decision facts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock

from fastapi import HTTPException

from app.personal_decision_contracts import BrokerPortfolioSnapshotInput, PersonalTradePlanInput
from app.personal_decision_repository import ImmutableDecisionFactConflict, persist_broker_snapshot, persist_trade_plan
from app.routers.personal_decisions import PersonalDecisionDependencies, build_personal_decisions_router


NOW = datetime(2026, 9, 1, 7, 15, tzinfo=timezone.utc)


def snapshot() -> BrokerPortfolioSnapshotInput:
    return BrokerPortfolioSnapshotInput.model_validate({
        "account_key": "citics-primary", "source": "citics_mumu",
        "source_snapshot_key": "20260901-close", "observed_at": NOW.isoformat(),
        "verification": "verified_exact", "total_market_value": "2800",
        "positions": [{
            "symbol": "600030.SH", "name": "中信证券", "quantity": "100",
            "sellable_quantity": "100", "average_cost": "27", "market_price": "28",
            "market_value": "2800",
        }],
    })


def plan() -> PersonalTradePlanInput:
    return PersonalTradePlanInput.model_validate({
        "plan_key": "holding:600030.SH:20260901", "plan_kind": "holding",
        "symbol": "600030.SH", "name": "中信证券", "as_of_at": NOW.isoformat(),
        "valid_until": (NOW + timedelta(days=1)).isoformat(), "action": "hold",
        "exit_trigger": "跌破失效位且板块同步转弱时退出", "max_position_pct": "20",
        "rationale": ["券商板块相对强度仍在"], "evidence_refs": ["review:close:20260901"],
    })


class Result:
    def __init__(self, row=None): self.row = row
    def fetchone(self): return self.row


class WriteConnection:
    def __init__(self, existing=None):
        self.existing = existing
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if "SELECT snapshot_id,content_hash" in sql or "SELECT plan_id,content_hash" in sql:
            return Result(self.existing)
        if "RETURNING snapshot_id" in sql:
            return Result({"snapshot_id": "snapshot-1", "observed_at": NOW, "verification": "verified_exact"})
        if "RETURNING plan_id" in sql:
            return Result({"plan_id": "plan-1", "as_of_at": NOW, "valid_until": NOW + timedelta(days=1)})
        return Result()


class PersonalDecisionPersistenceTests(unittest.TestCase):
    def test_snapshot_write_is_immutable_and_idempotent(self) -> None:
        connection = WriteConnection()
        created = persist_broker_snapshot(connection, snapshot())
        self.assertEqual(created["status"], "created")
        self.assertTrue(any("broker_position_snapshots" in sql for sql, _ in connection.calls))

        existing = WriteConnection({
            "snapshot_id": "snapshot-1", "content_hash": created["content_hash"],
            "observed_at": NOW, "verification": "verified_exact",
        })
        self.assertEqual(persist_broker_snapshot(existing, snapshot())["status"], "idempotent")
        conflict = WriteConnection({
            "snapshot_id": "snapshot-1", "content_hash": "0" * 64,
            "observed_at": NOW, "verification": "verified_exact",
        })
        with self.assertRaises(ImmutableDecisionFactConflict):
            persist_broker_snapshot(conflict, snapshot())

    def test_trade_plan_is_terminal_immutable_fact(self) -> None:
        connection = WriteConnection()
        created = persist_trade_plan(connection, plan())
        self.assertEqual(created["status"], "created")
        self.assertTrue(any("personal_trade_plans" in sql for sql, _ in connection.calls))

    def test_router_keeps_broker_and_order_boundaries_explicit(self) -> None:
        class Transaction:
            def __enter__(self): return object()
            def __exit__(self, *_args): return False

        database = Mock()
        database.transaction.return_value = Transaction()

        async def latest_snapshot(_database, _account):
            return {"snapshot_id": "snapshot-1", "live_orders": False}

        async def latest_brief(_database, _account):
            return {"status": "ready", "boundary": "human_decision_support_only"}

        router = build_personal_decisions_router(PersonalDecisionDependencies(
            database=database, async_database=object(),
            persist_snapshot=lambda _connection, _payload: {"status": "created"},
            persist_plan=lambda _connection, _payload: {"status": "created"},
            latest_snapshot=latest_snapshot, latest_brief=latest_brief, latest_research=Mock(),
        ))
        snapshot_endpoint = next(route.endpoint for route in router.routes if route.path.endswith("portfolio-snapshots"))
        plan_endpoint = next(route.endpoint for route in router.routes if route.path.endswith("trade-plans"))
        self.assertFalse(asyncio.run(snapshot_endpoint(snapshot()))["live_orders"])
        self.assertFalse(asyncio.run(plan_endpoint(plan()))["live_orders"])

    def test_router_maps_immutable_conflict_to_http_409(self) -> None:
        class Transaction:
            def __enter__(self): return object()
            def __exit__(self, *_args): return False

        database = Mock()
        database.transaction.return_value = Transaction()

        def conflict(_connection, _payload):
            raise ImmutableDecisionFactConflict("conflict")

        router = build_personal_decisions_router(PersonalDecisionDependencies(
            database=database, async_database=object(), persist_snapshot=conflict,
            persist_plan=conflict, latest_snapshot=Mock(), latest_brief=Mock(), latest_research=Mock(),
        ))
        endpoint = next(route.endpoint for route in router.routes if route.path.endswith("portfolio-snapshots"))
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(endpoint(snapshot()))
        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
