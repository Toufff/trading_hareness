from __future__ import annotations

import unittest

from app.ten_day_leader_rotation_research import classify_ten_day_coordination


class TenDayLeaderRotationResearchTests(unittest.TestCase):
    def _candidate(self, **overrides):
        return {
            "symbol": "300001.SZ",
            "board": "growth",
            "ten_day_rank": 8,
            "ten_day_return_pct": 62.0,
            "current_return_pct": 12.0,
            "is_limit_up": False,
            "is_one_word_board": False,
            "recently_suspended": False,
            **overrides,
        }

    def _cycle(self, **overrides):
        return {
            "state": "attack_accelerating",
            "strategy_available_at": "2026-08-17T01:35:00Z",
            **overrides,
        }

    def _minute(self, **overrides):
        return {
            "return_3m_pct": 0.8,
            "above_vwap_pct": 0.25,
            "minute_volume_multiple": 1.8,
            **overrides,
        }

    def _peers(self, **overrides):
        return {
            "exact_sector_mapping": True,
            "available_peer_count": 3,
            "confirming_peer_count": 2,
            "confirming_breadth": 2 / 3,
            "leader_limit_up": False,
            **overrides,
        }

    def test_confirms_external_and_internal_strength_without_creating_an_order(self) -> None:
        result = classify_ten_day_coordination(
            self._candidate(), self._cycle(), self._minute(), self._peers(),
        )

        self.assertEqual(result["shadow_state"], "confirmed_coordination")
        self.assertTrue(result["shadow_eligible"])
        self.assertFalse(result["decision_eligible"])
        self.assertEqual(result["scope"], "research_only_no_orders")
        self.assertEqual(result["candidate_path"], "ranked_expansion")

    def test_requires_exact_peer_or_leader_evidence_for_external_force(self) -> None:
        result = classify_ten_day_coordination(
            self._candidate(), self._cycle(), self._minute(),
            self._peers(exact_sector_mapping=False, confirming_peer_count=3, confirming_breadth=1.0),
        )

        self.assertEqual(result["shadow_state"], "external_force_unavailable")
        self.assertFalse(result["shadow_eligible"])
        self.assertIn("exact_sector_mapping_missing", result["risk_flags"])

    def test_one_word_board_is_observation_only_even_when_strength_confirms(self) -> None:
        result = classify_ten_day_coordination(
            self._candidate(is_limit_up=True, is_one_word_board=True, current_return_pct=20.0),
            self._cycle(), self._minute(), self._peers(leader_limit_up=True),
        )

        self.assertEqual(result["shadow_state"], "one_word_board_observation")
        self.assertFalse(result["shadow_eligible"])
        self.assertIn("one_word_board_not_entry", result["risk_flags"])

    def test_vwap_loss_and_negative_momentum_are_acceptance_failure(self) -> None:
        result = classify_ten_day_coordination(
            self._candidate(), self._cycle(),
            self._minute(above_vwap_pct=-0.2, return_3m_pct=-0.6), self._peers(),
        )

        self.assertEqual(result["shadow_state"], "acceptance_failure")
        self.assertFalse(result["shadow_eligible"])
        self.assertIn("vwap_acceptance_lost", result["reason_codes"])

    def test_board_expansion_thresholds_follow_the_workbook_taxonomy(self) -> None:
        main = classify_ten_day_coordination(
            self._candidate(board="main", current_return_pct=5.0), self._cycle(), self._minute(), self._peers(),
        )
        growth = classify_ten_day_coordination(
            self._candidate(board="growth", current_return_pct=9.99), self._cycle(), self._minute(), self._peers(),
        )
        beijing = classify_ten_day_coordination(
            self._candidate(board="bj", current_return_pct=15.0), self._cycle(), self._minute(), self._peers(),
        )

        self.assertEqual(main["candidate_path"], "ranked_expansion")
        self.assertEqual(growth["shadow_state"], "ranked_but_not_expanding")
        self.assertEqual(beijing["candidate_path"], "ranked_expansion")

    def test_kill_high_cycle_fails_closed(self) -> None:
        result = classify_ten_day_coordination(
            self._candidate(), self._cycle(state="kill_high"), self._minute(), self._peers(),
        )

        self.assertEqual(result["shadow_state"], "cycle_risk_blocked")
        self.assertFalse(result["shadow_eligible"])
        self.assertIn("cycle_not_supportive", result["reason_codes"])


if __name__ == "__main__":
    unittest.main()
