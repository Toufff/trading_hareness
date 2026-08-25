"""Runtime-boundary coverage for retained intraday outcome settlement."""

from __future__ import annotations

from datetime import date, datetime, timezone
import unittest

from app.intraday_outcome_runtime import IntradayOutcomeRuntime, IntradayOutcomeRuntimeDependencies


class _Transaction:
    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *_args):
        return None


class IntradayOutcomeRuntimeTests(unittest.TestCase):
    def test_recompute_uses_one_transaction_and_returns_attribution_receipt(self):
        connection = object()
        database = type("Database", (), {"transaction": lambda _self: _Transaction(connection)})()
        calls = []
        cutoff = datetime(2026, 8, 24, 7, 5, tzinfo=timezone.utc)

        def refresh(received_connection, *, cutoff):
            calls.append(("refresh", received_connection, cutoff))
            return 3

        def settle(received_connection, as_of_date, **kwargs):
            calls.append(("settle", received_connection, as_of_date, kwargs["cutoff"]))
            return {"status": "completed", "outcome_rows": 8}

        runtime = IntradayOutcomeRuntime(IntradayOutcomeRuntimeDependencies(
            database=database,
            outcome_cutoff=lambda _date: cutoff,
            refresh_attributions=refresh,
            settle=settle,
            horizons=(("5m", 5),),
            direction_for=lambda _signal_type: 1,
            metrics_for=lambda *_args: {},
            decimal_or_none=lambda value: value,
            barrier_spec_type=lambda: object(),
            triple_barrier_label=lambda *_args, **_kwargs: {},
            persist_barrier_outcome=lambda *_args, **_kwargs: None,
            return_decomposition=lambda *_args: {},
            json_safe=lambda value: value,
        ))

        result = runtime.recompute(date(2026, 8, 24))

        self.assertEqual(result, {"status": "completed", "outcome_rows": 8, "attribution_backfilled": 3})
        self.assertEqual(calls, [
            ("refresh", connection, cutoff),
            ("settle", connection, date(2026, 8, 24), cutoff),
        ])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
