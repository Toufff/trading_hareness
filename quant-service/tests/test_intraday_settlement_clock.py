"""Regression coverage for continuous-session intraday settlement bounds."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import unittest
from unittest.mock import MagicMock

from app.intraday_attribution import signal_attribution
from app.intraday_clock import continuous_auction_bounds, intraday_outcome_window
from app.intraday_outcome_settlement import settle
from app.intraday_outcomes import a_share_return_decomposition, intraday_signal_outcome_metrics
from app.paper_execution import triple_barrier_label
from app.strategy_contracts import LabelSpec


class IntradaySettlementClockTests(unittest.TestCase):
    def test_morning_target_at_close_is_allowed_but_lunch_crossing_is_unavailable(self) -> None:
        # 11:25 Asia/Shanghai.
        entry = datetime(2026, 8, 11, 3, 25, tzinfo=timezone.utc)
        cutoff = datetime(2026, 8, 11, 3, 40, tzinfo=timezone.utc)

        at_close = intraday_outcome_window(entry, horizon_minutes=5, cutoff=cutoff)
        crosses_lunch = intraday_outcome_window(entry, horizon_minutes=15, cutoff=cutoff)

        self.assertEqual(at_close["status"], "unavailable")
        self.assertEqual(at_close["reason"], "exit_quote_missing_within_tolerance")
        self.assertEqual(at_close["target_at"], datetime(2026, 8, 11, 3, 30, tzinfo=timezone.utc))
        self.assertEqual(crosses_lunch["status"], "unavailable")
        self.assertEqual(crosses_lunch["reason"], "target_crosses_continuous_session_boundary")

    def test_target_before_cutoff_stays_pending_only_inside_its_tolerance(self) -> None:
        entry = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)  # 09:00? outside
        self.assertEqual(intraday_outcome_window(entry, horizon_minutes=5, cutoff=entry)["status"], "unavailable")

        entry = datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc)  # 10:00 Asia/Shanghai.
        before_target = intraday_outcome_window(entry, horizon_minutes=5, cutoff=datetime(2026, 8, 11, 2, 4, tzinfo=timezone.utc))
        awaiting_quote = intraday_outcome_window(entry, horizon_minutes=5, cutoff=datetime(2026, 8, 11, 2, 5, 30, tzinfo=timezone.utc))
        expired = intraday_outcome_window(entry, horizon_minutes=5, cutoff=datetime(2026, 8, 11, 2, 6, 31, tzinfo=timezone.utc))

        self.assertEqual(before_target["status"], "pending")
        self.assertEqual(before_target["reason"], "target_not_yet_observable")
        self.assertEqual(awaiting_quote["status"], "pending")
        self.assertEqual(awaiting_quote["reason"], "awaiting_exit_quote_within_tolerance")
        self.assertEqual(expired["status"], "unavailable")
        self.assertEqual(expired["reason"], "exit_quote_missing_within_tolerance")

    def test_continuous_bounds_do_not_treat_lunch_or_overnight_as_trade_time(self) -> None:
        self.assertIsNone(continuous_auction_bounds(datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)))  # noon China.
        self.assertIsNone(continuous_auction_bounds(datetime(2026, 8, 11, 16, 0, tzinfo=timezone.utc)))
        afternoon = continuous_auction_bounds(datetime(2026, 8, 11, 5, 0, tzinfo=timezone.utc))
        self.assertIsNotNone(afternoon)
        self.assertEqual(afternoon[0], datetime(2026, 8, 11, 5, 0, tzinfo=timezone.utc))

    def test_non_eac_signal_with_shared_assessment_is_not_misattributed_as_eac(self) -> None:
        attribution = signal_attribution(
            "000001.SZ:watch:extreme_flow_buy", "watch",
            {"upside_research_assessment": {"status": "candidate"}}, {},
            number=lambda value: float(value) if value is not None else None,
            signal_model_version="watchlist-confirmation-v4",
        )
        self.assertEqual(attribution["stage"], "extension_watch")
        self.assertEqual(attribution["model_version"], "legacy-unversioned")
        self.assertEqual(attribution["volume_baseline"], "not_applicable")

    def test_settlement_does_not_query_an_afternoon_or_overnight_quote_for_lunch_crossing_horizon(self) -> None:
        signal_at = datetime(2026, 8, 11, 3, 25, tzinfo=timezone.utc)  # 11:25 China.
        signal = {
            "signal_event_id": "signal-1", "symbol": "000001.SZ", "signal_type": "entry",
            "observed_at": signal_at, "evidence": {"tencent": {"price": "10.00"}},
        }

        class Result:
            def __init__(self, *, rows=None, row=None):
                self.rows, self.row = rows or [], row

            def fetchall(self):
                return self.rows

            def fetchone(self):
                return self.row

        calls: list[tuple[str, tuple[object, ...] | None]] = []

        class Connection:
            def execute(self, query, params=None):
                calls.append((str(query), params))
                if "FROM quant.intraday_signal_events" in query:
                    return Result(rows=[signal])
                if "SELECT observed_at,price" in query:
                    return Result(rows=[])
                if "FROM quant.canonical_bars_daily" in query:
                    return Result(row=None)
                return Result()

        persist_barrier = MagicMock()
        result = settle(
            Connection(), date(2026, 8, 11), cutoff=datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc),
            horizons=(("15m", 15),), direction_for=lambda _signal_type: 1,
            metrics_for=intraday_signal_outcome_metrics,
            decimal_or_none=lambda value: Decimal(str(value)) if value is not None else None,
            barrier_spec_type=LabelSpec, triple_barrier_label=triple_barrier_label,
            persist_barrier_outcome=persist_barrier, return_decomposition=a_share_return_decomposition,
            json_safe=lambda value: value,
        )

        self.assertEqual(result["matured"], 0)
        self.assertEqual(result["pending"], 2)  # the two daily references remain pending.
        self.assertFalse(any("observed_at>=%s AND observed_at<=%s" in query for query, _ in calls))
        outcome_insert = next(params for query, params in calls if "INSERT INTO quant.intraday_signal_outcomes" in query)
        self.assertEqual(outcome_insert[10], "unavailable")
        self.assertEqual(persist_barrier.call_args.kwargs["result"]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
