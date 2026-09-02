"""Coverage for the WP10 fixes to sector_flow_repository.

``materialize_sector_flow_daily_outcomes`` previously read the entire history
of two tables on every call (no lower bound) and wrote one INSERT per
(sector, day, horizon) outcome row.  It now bounds the read to a configurable
lookback window and writes the whole batch through one ``unnest``-driven
upsert.
"""

from __future__ import annotations

from datetime import date
import unittest

from app.sector_flow_repository import (
    DEFAULT_OUTCOME_LOOKBACK_DAYS,
    materialize_sector_flow_daily_outcomes,
)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, feature_rows, price_rows):
        self.feature_rows = feature_rows
        self.price_rows = price_rows
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        if "FROM quant.sector_flow_daily_features" in normalized:
            return _Result(self.feature_rows)
        if "FROM quant.sector_market_observations" in normalized:
            return _Result(self.price_rows)
        return _Result([])


class _Transaction:
    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *_args):
        return False


class _Database:
    def __init__(self, feature_rows, price_rows):
        self.connection = _Connection(feature_rows, price_rows)

    def transaction(self):
        return _Transaction(self.connection)


class MaterializeSectorFlowOutcomesTests(unittest.TestCase):
    def test_reads_are_bounded_by_a_configurable_lookback_window(self) -> None:
        database = _Database(feature_rows=[], price_rows=[])
        as_of_date = date(2026, 8, 20)
        materialize_sector_flow_daily_outcomes(database, as_of_date, lookback_days=30)
        reads = [(sql, params) for sql, params in database.connection.calls
                 if "sector_flow_daily_features" in sql or "sector_market_observations" in sql]
        self.assertEqual(len(reads), 2)
        for _sql, params in reads:
            self.assertEqual(params[0], as_of_date)
            self.assertEqual(params[1], date(2026, 7, 21))

    def test_default_lookback_matches_the_documented_constant(self) -> None:
        self.assertEqual(DEFAULT_OUTCOME_LOOKBACK_DAYS, 120)

    def test_outcomes_are_written_in_one_batched_statement(self) -> None:
        feature_rows = [
            {"taxonomy_key": "ths_concept_flow", "sector_key": "885001.TI",
             "trading_date": date(2026, 8, 18), "transition": "persistent_in"},
            {"taxonomy_key": "ths_concept_flow", "sector_key": "885002.TI",
             "trading_date": date(2026, 8, 18), "transition": "persistent_out"},
        ]
        price_rows = [
            {"trading_date": date(2026, 8, 18), "sector_key": "885001.TI", "close": 10.0, "available_at": None},
            {"trading_date": date(2026, 8, 19), "sector_key": "885001.TI", "close": 11.0, "available_at": None},
            {"trading_date": date(2026, 8, 18), "sector_key": "885002.TI", "close": 20.0, "available_at": None},
            {"trading_date": date(2026, 8, 19), "sector_key": "885002.TI", "close": 19.0, "available_at": None},
        ]
        database = _Database(feature_rows=feature_rows, price_rows=price_rows)
        result = materialize_sector_flow_daily_outcomes(database, date(2026, 8, 20))
        # Two sectors x 3 horizons (1, 3, 5) = 6 provisional outcome rows.
        self.assertEqual(result["rows"], 6)
        insert_calls = [(sql, params) for sql, params in database.connection.calls
                        if "INSERT INTO quant.sector_flow_daily_outcomes" in sql]
        self.assertEqual(len(insert_calls), 1)
        _sql, params = insert_calls[0]
        self.assertEqual(len(params[0]), 6)

    def test_no_provisional_rows_issues_no_write(self) -> None:
        database = _Database(feature_rows=[], price_rows=[])
        result = materialize_sector_flow_daily_outcomes(database, date(2026, 8, 20))
        self.assertEqual(result["rows"], 0)
        self.assertFalse(any("INSERT INTO quant.sector_flow_daily_outcomes" in sql
                             for sql, _params in database.connection.calls))


if __name__ == "__main__":
    unittest.main()
