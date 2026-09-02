from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from typing import Any

from app.decision_research_repository import holding_evidence


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
