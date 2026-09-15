from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from typing import Any

from app.decision_research_repository import holding_evidence, latest_candidate_evidence


class _Result:
    def __init__(self, rows: list[dict[str, Any]] | None = None, row: dict[str, Any] | None = None) -> None:
        self._rows, self._row = rows or [], row

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

    def fetchone(self) -> dict[str, Any] | None:
        return self._row


class _Connection:
    def __init__(self, bars: list[dict[str, Any]]) -> None:
        self._bars = bars

    def execute(self, sql: str, _params: tuple[Any, ...]) -> _Result:
        if "FROM quant.instruments" in sql:
            return _Result(row={"name": "示例", "daily_basic": None, "basic_available_at": None,
                                 "main_net_amount": None, "flow_raw": None, "flow_available_at": None,
                                 "board_context": None, "amount": None, "close": None, "pre_close": None,
                                 "volume": None, "bar_available_at": None})
        if "FROM quant.canonical_bars_daily" in sql:
            return _Result(rows=self._bars)
        if "FROM quant.legacy_source_records" in sql:
            return _Result(row=None)
        raise AssertionError(f"unexpected SQL: {sql}")


def _bar(day: int, close: float, adj_factor: float) -> dict[str, Any]:
    trading_date = date(2026, 1, day)
    return {
        "trading_date": trading_date, "open": Decimal(str(close)), "high": Decimal(str(close + 0.2)),
        "low": Decimal(str(close - 0.2)), "close": Decimal(str(close)), "volume": Decimal("1000"),
        "adj_factor": Decimal(str(adj_factor)),
    }


class HoldingEvidenceUsesCanonicalBarsTests(unittest.TestCase):
    def test_bars_are_adjusted_with_the_real_incremental_factor(self) -> None:
        # Descending, matching the query's ORDER BY trading_date DESC.
        bars = [_bar(3, 12.0, 2.0), _bar(2, 11.0, 2.0), _bar(1, 10.0, 2.0)]
        connection = _Connection(bars)
        evidence = holding_evidence(connection, date(2026, 1, 3), "000001.SZ")
        self.assertIsNotNone(evidence)
        # reversed() back to ascending order by the function's own return.
        self.assertEqual([bar["trading_date"] for bar in evidence["bars"]], [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)])
        # research_close = raw_close * adj_factor, published under "close".
        self.assertEqual(evidence["bars"][-1]["close"], 24.0)
        self.assertEqual(evidence["bars"][0]["close"], 20.0)

    def test_missing_adj_factor_fails_closed_to_no_bars(self) -> None:
        bars = [_bar(2, 11.0, 2.0), {**_bar(1, 10.0, 2.0), "adj_factor": None}]
        connection = _Connection(bars)
        evidence = holding_evidence(connection, date(2026, 1, 2), "000001.SZ")
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["bars"], [])


if __name__ == "__main__":
    unittest.main()


class LaneCandidateEvidenceTests(unittest.TestCase):
    def test_nine_lane_review_plan_precedes_legacy_candidate_table(self) -> None:
        planned = [{
            "run_id": "run-1", "rank": 0, "symbol": "002185.SZ", "name": "华天科技",
            "candidate_type": "strategy_lane:accumulation", "score": Decimal("66.25"),
            "structure": {"metrics": {"close": 16.36}}, "board_context": {},
            "risk_flags": [], "daily_basic": {}, "basic_available_at": None,
            "main_net_amount": Decimal("1"), "flow_raw": {}, "flow_available_at": None,
            "amount": Decimal("10"), "close": Decimal("16.36"), "pre_close": Decimal("16"),
            "volume": Decimal("1"), "bar_available_at": None,
        }]

        class Connection:
            calls = 0

            def execute(self, sql: str, _params: tuple[Any, ...]) -> _Result:
                self.calls += 1
                self.last_sql = sql
                if "jsonb_array_elements" in sql:
                    return _Result(rows=planned)
                raise AssertionError("legacy single-strategy query must not run when lane plan exists")

        connection = Connection()
        result = latest_candidate_evidence(connection, date(2026, 9, 15), 12)
        self.assertEqual([row["symbol"] for row in result], ["002185.SZ"])
        self.assertIn("strategy_lanes", connection.last_sql)
        self.assertEqual(connection.calls, 1)

    def test_old_dates_without_lane_plan_keep_bounded_legacy_fallback(self) -> None:
        class Connection:
            calls = 0

            def execute(self, sql: str, _params: tuple[Any, ...]) -> _Result:
                self.calls += 1
                if "jsonb_array_elements" in sql:
                    return _Result(rows=[])
                if "SELECT 1 FROM quant.post_close_strategy_runs" in sql:
                    return _Result(row=None)
                self.legacy_sql = sql
                return _Result(rows=[{"symbol": "600001.SH"}])

        connection = Connection()
        result = latest_candidate_evidence(connection, date(2026, 8, 1), 12)
        self.assertEqual(result, [{"symbol": "600001.SH"}])
        self.assertIn("post_close_strategy_candidates", connection.legacy_sql)
        self.assertEqual(connection.calls, 3)

    def test_empty_current_lane_plan_does_not_resurrect_legacy_candidates(self) -> None:
        class Connection:
            def execute(self, sql: str, _params: tuple[Any, ...]) -> _Result:
                if "jsonb_array_elements" in sql:
                    return _Result(rows=[])
                if "SELECT 1 FROM quant.post_close_strategy_runs" in sql:
                    return _Result(row={"?column?": 1})
                raise AssertionError("legacy candidates must remain isolated")

        self.assertEqual(latest_candidate_evidence(Connection(), date(2026, 9, 15), 12), [])
