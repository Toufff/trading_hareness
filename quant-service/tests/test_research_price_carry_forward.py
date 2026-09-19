"""The trailing-gap carry-forward rule and the consumers that read it.

The systemic condition this covers: the tushare factor fetch is a separate
evening lane, so on a longhu evening the newest sessions land with
``adj_factor IS NULL``.  Before this rule every cross-session consumer failed
closed on those sessions and the recommendation score was penalised for a
platform data gap that has nothing to do with the symbol -- which is how a
better stock gets pushed out of the pool.  The rule is deliberately narrow:
trailing gap only, bounded, and only while the bars themselves show no
corporate action.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.research_prices import (
    ADJUSTMENT_MISSING_FLAG,
    CARRIED_FORWARD_FLAG,
    CORPORATE_ACTION_UNRESOLVED_FLAG,
    MAX_CARRIED_FACTOR_SESSIONS,
    adjusted_bars,
    adjusted_value,
    carried_forward_sessions,
    resolve_factors,
)


def bars(factors, *, closes=None, pre_closes=None, start=date(2026, 9, 1)):
    """Build a continuous window: every ``pre_close`` is the previous close."""
    closes = list(closes if closes is not None else [10.0 + index * 0.1 for index in range(len(factors))])
    if pre_closes is None:
        pre_closes = [closes[0]] + closes[:-1]
    return [
        {"trading_date": start + timedelta(days=index), "open": closes[index], "high": closes[index] + 0.2,
         "low": closes[index] - 0.2, "close": closes[index], "pre_close": pre_closes[index],
         "volume": 1000 + index, "adj_factor": factors[index]}
        for index in range(len(factors))
    ]


class ResolveFactorsTests(unittest.TestCase):
    def test_a_complete_window_is_unchanged_and_unflagged(self):
        resolution = resolve_factors(bars([1.2, 1.2, 1.25]))
        self.assertEqual(resolution.factors, (1.2, 1.2, 1.25))
        self.assertEqual(resolution.flags, ())
        self.assertEqual(resolution.carried_sessions, 0)

    def test_a_trailing_gap_carries_the_last_real_factor(self):
        resolution = resolve_factors(bars([1.3, 1.3, None, None]))
        self.assertEqual(resolution.factors, (1.3, 1.3, 1.3, 1.3))
        self.assertEqual(resolution.flags, (CARRIED_FORWARD_FLAG,))
        self.assertEqual(resolution.carried_sessions, 2)

    def test_the_bound_is_five_sessions_and_the_sixth_fails_closed(self):
        self.assertEqual(MAX_CARRIED_FACTOR_SESSIONS, 5)
        at_bound = resolve_factors(bars([1.1] + [None] * MAX_CARRIED_FACTOR_SESSIONS))
        self.assertEqual(at_bound.carried_sessions, MAX_CARRIED_FACTOR_SESSIONS)
        self.assertEqual(at_bound.flags, (CARRIED_FORWARD_FLAG,))
        beyond = resolve_factors(bars([1.1] + [None] * (MAX_CARRIED_FACTOR_SESSIONS + 1)))
        self.assertIsNone(beyond.factors)
        self.assertEqual(beyond.flags, (ADJUSTMENT_MISSING_FLAG,))

    def test_an_ex_rights_signature_inside_the_gap_is_refused(self):
        # The exchange republishes pre_close at the adjusted level, so the
        # third session opens its book 1.00 below the previous close: that is
        # a corporate action, and carrying the old factor would be a guess.
        window = bars([1.4, 1.4, None, None], closes=[10.0, 10.2, 9.2, 9.3],
                      pre_closes=[10.0, 10.0, 9.2, 9.2])
        resolution = resolve_factors(window)
        self.assertIsNone(resolution.factors)
        self.assertEqual(resolution.flags, (CORPORATE_ACTION_UNRESOLVED_FLAG,))

    def test_a_one_fen_difference_is_rounding_and_still_carries(self):
        window = bars([1.4, 1.4, None], closes=[10.0, 10.2, 10.3],
                      pre_closes=[10.0, 10.0, 10.21])
        self.assertEqual(resolve_factors(window).flags, (CARRIED_FORWARD_FLAG,))

    def test_an_interior_gap_is_refused_even_with_continuous_prices(self):
        # Real factors exist AFTER the hole, so the fetch lane is not behind;
        # this is missing history and it must keep failing closed.
        resolution = resolve_factors(bars([1.5, None, 1.5, 1.5]))
        self.assertIsNone(resolution.factors)
        self.assertEqual(resolution.flags, (ADJUSTMENT_MISSING_FLAG,))

    def test_a_window_without_any_real_factor_is_refused(self):
        resolution = resolve_factors(bars([None, None, None]))
        self.assertIsNone(resolution.factors)
        self.assertEqual(resolution.flags, (ADJUSTMENT_MISSING_FLAG,))

    def test_a_missing_pre_close_proves_nothing_and_is_refused(self):
        window = bars([1.6, 1.6, None])
        window[2]["pre_close"] = None
        resolution = resolve_factors(window)
        self.assertIsNone(resolution.factors)
        self.assertEqual(resolution.flags, (ADJUSTMENT_MISSING_FLAG,))

    def test_a_non_positive_factor_counts_as_missing_not_as_a_basis(self):
        resolution = resolve_factors(bars([1.7, 1.7, 0]))
        self.assertEqual(resolution.factors, (1.7, 1.7, 1.7))
        self.assertEqual(resolution.carried_sessions, 1)

    def test_an_empty_window_is_neither_carried_nor_refused(self):
        resolution = resolve_factors([])
        self.assertEqual(resolution.factors, ())
        self.assertEqual(resolution.flags, ())


class AdjustedBarsTests(unittest.TestCase):
    def test_carried_rows_are_prepared_on_the_carried_factor_and_marked(self):
        window = bars([1.2, 1.2, None])
        prepared, flags = adjusted_bars(window)
        self.assertEqual(flags, [CARRIED_FORWARD_FLAG])
        self.assertIsNotNone(prepared)
        self.assertEqual([row["research_close"] for row in prepared],
                         [row["close"] * 1.2 for row in window])
        self.assertEqual(carried_forward_sessions(prepared), 1)
        self.assertNotIn("research_adj_factor_carried", prepared[0])
        self.assertTrue(prepared[-1]["research_adj_factor_carried"])
        self.assertEqual(prepared[-1]["research_adj_factor"], 1.2)

    def test_a_refused_window_still_returns_none_with_the_hard_flag(self):
        prepared, flags = adjusted_bars(bars([1.2, None, 1.2]))
        self.assertIsNone(prepared)
        self.assertEqual(flags, [ADJUSTMENT_MISSING_FLAG])

    def test_a_complete_window_reports_no_carried_sessions(self):
        prepared, flags = adjusted_bars(bars([1.2, 1.2, 1.2]))
        self.assertEqual(flags, [])
        self.assertEqual(carried_forward_sessions(prepared), 0)

    def test_a_single_row_value_stays_strict(self):
        # One row carries no neighbouring close to test continuity against, so
        # the per-row contract is unchanged.
        self.assertIsNone(adjusted_value({"close": 10.0, "adj_factor": None}))
        self.assertEqual(adjusted_value({"close": 10.0, "adj_factor": 1.5}), 15.0)


class CarriedForwardConsumerTests(unittest.TestCase):
    def test_post_close_structures_report_the_flag_instead_of_blocking(self):
        from app.post_close_structures import daily_base_structure

        window = bars([1.1] * 28 + [None, None], closes=[10.0 + index * 0.01 for index in range(30)])
        result = daily_base_structure(window)
        self.assertNotEqual(result["status"], "data_quality_blocked")
        self.assertEqual(result["quality_flags"], [CARRIED_FORWARD_FLAG])

    def test_post_close_structures_still_block_on_an_ex_rights_signature(self):
        closes = [10.0] * 28 + [9.0, 9.0]
        pre_closes = [10.0] * 28 + [9.0, 9.0]
        window = bars([1.1] * 28 + [None, None], closes=closes, pre_closes=pre_closes)
        result = daily_base_structure_status(window)
        self.assertEqual(result["status"], "data_quality_blocked")
        self.assertEqual(result["quality_flags"], [CORPORATE_ACTION_UNRESOLVED_FLAG])

    def test_ten_day_ranking_keeps_a_symbol_whose_factor_lane_is_behind(self):
        from app.ten_day_leader_ranking import rank_ten_day_candidates

        as_of = date(2026, 9, 18)
        rows = []
        for index in range(11):
            trading_date = as_of - timedelta(days=10 - index)
            close = 10.0 + index * 0.5
            rows.append({"symbol": "600000.SH", "name": "样本", "trading_date": trading_date,
                         "open": close, "high": close, "low": close, "close": close,
                         "pre_close": 10.0 + (index - 1) * 0.5 if index else close,
                         "adj_factor": 1.3 if index < 9 else None,
                         "limit_up": close * 1.1, "is_suspended": False})
        result = rank_ten_day_candidates(rows, as_of, daily_symbols=1, minimum_full_market_symbols=1)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["source_status"]["eligible_symbols"], 1)
        self.assertEqual(result["source_status"]["carried_forward_factor_symbols"], 1)

    def test_effectiveness_simulation_runs_on_a_carried_window(self):
        from app.effectiveness.execution import simulate

        sessions = ["2026-09-15", "2026-09-16", "2026-09-17"]
        rows = []
        for index, session in enumerate(sessions):
            close = 10.0 + index * 0.1
            rows.append({"date": session, "open": close, "high": close + 0.2, "low": close - 0.2,
                         "close": close, "pre_close": 10.0 + (index - 1) * 0.1 if index else close,
                         "limit_up": close * 1.1, "limit_down": close * 0.9, "is_suspended": False,
                         "adj_factor": 1.25 if index == 0 else None})
        result = simulate(rows, sessions)
        self.assertEqual(result["status"], "simulated")
        self.assertEqual(result["adjustment_basis"], "carried_forward")
        self.assertEqual(result["carried_factor_sessions"], 2)
        self.assertEqual(result["quality_flags"], [CARRIED_FORWARD_FLAG])

    def test_effectiveness_simulation_still_refuses_an_ex_rights_window(self):
        from app.effectiveness.execution import simulate

        sessions = ["2026-09-15", "2026-09-16"]
        rows = [
            {"date": sessions[0], "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.0, "pre_close": 10.0,
             "limit_up": 11.0, "limit_down": 9.0, "is_suspended": False, "adj_factor": 1.25},
            {"date": sessions[1], "open": 9.0, "high": 9.2, "low": 8.8, "close": 9.0, "pre_close": 9.0,
             "limit_up": 9.9, "limit_down": 8.1, "is_suspended": False, "adj_factor": None},
        ]
        self.assertEqual(simulate(rows, sessions)["status"], "corporate_action_unmodeled")

    def test_the_intraday_daily_factor_contract_declares_the_carried_state(self):
        from app.intraday_factor_contracts import FACTOR_SPECS

        self.assertIn(CARRIED_FORWARD_FLAG, FACTOR_SPECS["daily_rebound_state"].quality_flags)
        self.assertIn(ADJUSTMENT_MISSING_FLAG, FACTOR_SPECS["daily_rebound_state"].quality_flags)


def daily_base_structure_status(window):
    from app.post_close_structures import daily_base_structure

    return daily_base_structure(window)


if __name__ == "__main__":
    unittest.main()
