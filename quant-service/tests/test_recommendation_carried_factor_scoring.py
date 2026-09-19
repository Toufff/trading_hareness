"""A lagging factor lane must not cost a candidate points.

``adj_factor_carried_forward`` describes the *platform's* fetch state, not the
symbol.  Penalising it demotes the whole pool on exactly the evenings when the
tushare lane is behind, and a genuinely better stock loses to a worse one that
happened to be scored on a complete window.  ``adj_factor_missing`` -- no
usable basis at all -- stays hard, because there the cross-session numbers do
not exist.
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

from app.recommendation_generation import UNPENALIZED_FLAGS, generate
from app.research_prices import ADJUSTMENT_MISSING_FLAG, CARRIED_FORWARD_FLAG
from app.strategy_ablation import ablation_scores


class _Connection:
    def __init__(self):
        self.statements = []

    def execute(self, sql, parameters=None):
        self.statements.append((sql, parameters))
        return self


class _Database:
    def __init__(self):
        self.connection = _Connection()

    @contextmanager
    def transaction(self):
        yield self.connection


def _number(value, default=None):
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _item(symbol, flags):
    return {
        "symbol": symbol,
        "quality_flags": list(flags),
        "features": {"symbol": symbol, "close": 12.0, "sma_20": 10.0, "return_5": 0.05,
                     "return_20": 0.09, "bar_count": 60, "market_data_date": "2026-09-18",
                     "analyst": {}},
    }


def _run(items):
    database = _Database()
    request = SimpleNamespace(as_of_date=date(2026, 9, 18), universe_key="all_a", horizon_days=5, limit=10)
    result = generate(
        request,
        cn_today=lambda: date(2026, 9, 18),
        build_feature_snapshot=lambda _date, _universe: {
            "market_regime": "neutral", "snapshot_key": "snapshot", "items": items},
        analyst_execution_context=lambda _connection, _date: {
            "execution_eligible": False, "max_live_weight": 0.0, "status": "disabled"},
        ablation_scores=ablation_scores, number=_number, db=database,
        model_version="test-model", feature_version="test-features", json_safe=lambda value: value,
    )
    return {candidate["symbol"]: candidate for candidate in result["recommendations"]}


class CarriedFactorScoringTests(unittest.TestCase):
    def test_a_carried_forward_window_scores_exactly_like_a_complete_one(self):
        scored = _run([_item("600000.SH", []), _item("600001.SH", [CARRIED_FORWARD_FLAG])])
        self.assertEqual(scored["600001.SH"]["score"], scored["600000.SH"]["score"])
        self.assertEqual(scored["600001.SH"]["market_only_score"], scored["600000.SH"]["market_only_score"])
        self.assertEqual(scored["600000.SH"]["decision"], "research_candidate")
        self.assertEqual(scored["600001.SH"]["decision"], "research_candidate")
        # Recorded, not hidden: the reader still sees the basis.
        self.assertIn(CARRIED_FORWARD_FLAG, scored["600001.SH"]["flags"])

    def test_a_missing_factor_is_still_hard_and_still_penalised(self):
        scored = _run([_item("600000.SH", []), _item("600002.SH", [ADJUSTMENT_MISSING_FLAG])])
        self.assertLess(scored["600002.SH"]["score"], scored["600000.SH"]["score"])
        self.assertEqual(scored["600002.SH"]["decision"], "watch")

    def test_a_carried_flag_does_not_shield_a_real_flag_from_the_penalty(self):
        scored = _run([_item("600000.SH", ["stale_market_data"]),
                       _item("600003.SH", ["stale_market_data", CARRIED_FORWARD_FLAG])])
        self.assertEqual(scored["600003.SH"]["score"], scored["600000.SH"]["score"])
        self.assertLess(scored["600003.SH"]["score"], _run([_item("600000.SH", [])])["600000.SH"]["score"])

    def test_the_unpenalized_set_is_exactly_the_carried_forward_flag(self):
        self.assertEqual(set(UNPENALIZED_FLAGS), {CARRIED_FORWARD_FLAG})


if __name__ == "__main__":
    unittest.main()
