from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from app.ten_day_leader_rotation_repository import TenDayRankingInputs
from app.ten_day_leader_rotation_service import TenDayLeaderRotationDependencies, run_ten_day_leader_rotation


class TenDayLeaderRotationServiceTests(unittest.TestCase):
    def test_materializes_ranked_shadow_observations_without_order_eligibility(self) -> None:
        available = datetime(2026, 8, 13, 8, tzinfo=timezone.utc)
        ranked = {"status": "completed", "reason": None, "source_status": {"eligible_symbols": 5001},
                  "candidates": [{"symbol": "600001.SH", "board": "main", "ten_day_rank": 1,
                                  "ten_day_return_pct": 20, "current_return_pct": 10}]}
        classified = {"candidate_path": "ranked_expansion", "shadow_state": "cycle_context_unavailable",
                      "shadow_eligible": False, "decision_eligible": False, "evidence": {},
                      "reason_codes": ["strategy_available_at_missing"], "risk_flags": ["no_automatic_order"]}
        persist = MagicMock(return_value="run-1")
        dependencies = TenDayLeaderRotationDependencies(
            latest_full_market_date=lambda minimum: date(2026, 8, 13),
            load_inputs=lambda as_of: TenDayRankingInputs([], 5001, available),
            rank_candidates=MagicMock(return_value=ranked),
            classify=MagicMock(return_value=classified),
            persist=persist,
            json_safe=lambda value: value,
        )

        result = run_ten_day_leader_rotation(
            SimpleNamespace(as_of_date=None, minimum_full_market_symbols=5000, per_board_limit=30),
            dependencies,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["run_id"], "run-1")
        self.assertFalse(result["candidates"][0]["decision_eligible"])
        dependencies.classify.assert_called_once_with(ranked["candidates"][0], None, None, None)
        self.assertEqual(persist.call_args.kwargs["strategy_available_at"], available)

    def test_does_not_persist_when_no_full_market_date_exists(self) -> None:
        persist = MagicMock()
        result = run_ten_day_leader_rotation(
            SimpleNamespace(as_of_date=None, minimum_full_market_symbols=5000, per_board_limit=30),
            TenDayLeaderRotationDependencies(
                latest_full_market_date=lambda minimum: None,
                load_inputs=MagicMock(), rank_candidates=MagicMock(), classify=MagicMock(),
                persist=persist, json_safe=lambda value: value,
            ),
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "no_full_market_daily_date")
        persist.assert_not_called()

    def test_explicit_date_cannot_bypass_point_in_time_coverage_gate(self) -> None:
        rank = MagicMock()
        persist = MagicMock()
        result = run_ten_day_leader_rotation(
            SimpleNamespace(as_of_date=date(2026, 8, 21), minimum_full_market_symbols=5000, per_board_limit=30),
            TenDayLeaderRotationDependencies(
                latest_full_market_date=MagicMock(),
                load_inputs=lambda _as_of: TenDayRankingInputs([], 5_000, None, expected_daily_symbols=5_600),
                rank_candidates=rank, classify=MagicMock(), persist=persist, json_safe=lambda value: value,
            ),
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "incomplete_full_market_daily_coverage")
        self.assertEqual(result["source_status"]["minimum_required_symbols"], 5_320)
        rank.assert_not_called()
        persist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
