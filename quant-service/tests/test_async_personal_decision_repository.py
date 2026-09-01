"""Freshness behavior for the native-async personal decision read model."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from app.async_personal_decision_repository import active_trade_plans, latest_decision_research, latest_market_section


NOW = datetime(2026, 9, 1, 7, 15, tzinfo=timezone.utc)


class Result:
    def __init__(self, row): self.row = row
    async def fetchone(self): return self.row
    async def fetchall(self): return self.row


class Connection:
    def __init__(self, row): self.row = row
    async def execute(self, _sql, _params=()): return Result(self.row)


class Transaction:
    def __init__(self, row): self.connection = Connection(row)
    async def __aenter__(self): return self.connection
    async def __aexit__(self, *_args): return False


class Database:
    def __init__(self, row): self.row = row
    def transaction(self): return Transaction(self.row)


class SequencedConnection:
    def __init__(self, rows): self.rows = list(rows)
    async def execute(self, _sql, _params=()): return Result(self.rows.pop(0))


class SequencedTransaction:
    def __init__(self, rows): self.connection = SequencedConnection(rows)
    async def __aenter__(self): return self.connection
    async def __aexit__(self, *_args): return False


class SequencedDatabase:
    def __init__(self, rows): self.rows = rows
    def transaction(self): return SequencedTransaction(self.rows)


class CapturingDatabase:
    def __init__(self):
        self.sql = ""

    def transaction(self):
        database = self

        class CaptureTransaction:
            async def __aenter__(self):
                class CaptureConnection:
                    async def execute(self, sql, _params=()):
                        database.sql = sql
                        return Result([])
                return CaptureConnection()

            async def __aexit__(self, *_args): return False
        return CaptureTransaction()


class AsyncPersonalDecisionRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_market_section_has_an_explicit_age_boundary(self) -> None:
        base = {
            "review_id": "review-1", "exchange_date": NOW.date(), "session": "close",
            "market_state": "rotation", "data_boundary": {}, "report": {},
        }
        current = await latest_market_section(
            Database(base | {"observed_at": NOW - timedelta(hours=66)}), as_of_at=NOW,
        )
        self.assertEqual(current["status"], "ready")

        degraded = await latest_market_section(
            Database(base | {
                "observed_at": NOW - timedelta(hours=1),
                "report": {"index_breadth_context": {"quality_flags": ["missing_index_context"]}},
            }),
            as_of_at=NOW,
        )
        self.assertEqual(degraded["status"], "degraded")
        self.assertEqual(degraded["quality_flags"], ["missing_index_context"])

        stale = await latest_market_section(
            Database(base | {"observed_at": NOW - timedelta(days=5)}), as_of_at=NOW,
        )
        self.assertEqual(stale["status"], "unavailable")

        future = await latest_market_section(
            Database(base | {"observed_at": NOW + timedelta(hours=1)}), as_of_at=NOW,
        )
        self.assertEqual(future["status"], "unavailable")

    async def test_research_read_reports_only_the_selected_terminal_batch(self) -> None:
        items = [
            {
                "dossier_id": "holding-1", "dossier_key": "holding:600664", "as_of_date": NOW.date(),
                "symbol": "600664.SH", "name": "哈药股份", "strategy_family": "holding",
                "model_version": "short-term-decision-research-v2", "status": "passed",
                "conclusion": "形成持仓计划", "source_candidate_rank": None,
                "evidence_snapshot": {"role": "holding"}, "evidence_refs": [],
                "created_at": NOW, "gates": [{"gate_key": "G6", "label": "独立下行情景", "verdict": "pass"}],
            },
            {
                "dossier_id": "candidate-1", "dossier_key": "candidate:603305", "as_of_date": NOW.date(),
                "symbol": "603305.SH", "name": "旭升集团", "strategy_family": "short_term",
                "model_version": "short-term-decision-research-v2", "status": "rejected",
                "conclusion": "流动性门槛未通过", "source_candidate_rank": 2,
                "evidence_snapshot": {"role": "candidate"}, "evidence_refs": [],
                "created_at": NOW, "gates": [{"gate_key": "G5", "label": "价格结构、流动性与触发条件", "verdict": "fail"}],
            },
        ]
        result = await latest_decision_research(SequencedDatabase([
            {"as_of_date": NOW.date()}, items,
        ]))

        self.assertEqual(result["summary"], {"total": 2, "passed": 1, "rejected": 1, "incomplete": 0})
        self.assertEqual([item["name"] for item in result["items"]], ["哈药股份", "旭升集团"])
        self.assertTrue(result["boundary"].startswith("terminal research audit"))

    async def test_active_plan_supersession_prefers_latest_creation_over_legacy_clock(self) -> None:
        database = CapturingDatabase()
        self.assertEqual(await active_trade_plans(database, NOW), [])
        normalized = " ".join(database.sql.split())
        self.assertIn("ORDER BY plan_kind,symbol,created_at DESC,as_of_at DESC", normalized)


if __name__ == "__main__":
    unittest.main()
