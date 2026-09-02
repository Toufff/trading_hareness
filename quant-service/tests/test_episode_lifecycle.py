from datetime import datetime, timezone
import unittest

from app.episode_lifecycle import material_state_hash, signal_stage, strategy_family
from app.intraday_signal_policy import signal_event_state, signal_material_change


class EpisodeLifecycleTests(unittest.TestCase):
    def test_signal_policy_material_change_and_confirmation_are_pure(self):
        observed = datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc)
        signal = {"signal_type": "watch", "hard": False, "score": 80,
                  "conditions": {"price": 10.0, "volume_ratio": 2.0, "main_net_inflow": 1.0}}
        prior = {"score": 80, "conditions": {"price": 10.0, "volume_ratio": 2.0, "main_net_inflow": 1.0}}
        self.assertFalse(signal_material_change(signal, prior))
        self.assertTrue(signal_material_change({**signal, "conditions": {**signal["conditions"], "price": 10.2}}, prior))
        self.assertEqual(signal_event_state(signal, observed_at=observed, latest_event_at=None,
                                            last_key_alerted_at=None, last_symbol_watch_alerted_at=None), "confirming")
        self.assertEqual(signal_event_state({**signal, "alert_on_first_observation": True}, observed_at=observed,
                                            latest_event_at=None, last_key_alerted_at=None,
                                            last_symbol_watch_alerted_at=None), "confirmed")
    def test_stage_and_strategy_family_are_stable(self):
        self.assertEqual(strategy_family("000001.SZ:watch:upside_breakout_eac_v3"), "upside_breakout_eac")
        self.assertEqual(signal_stage({"signal_type": "watch", "conditions": {"setup": "deep_reversal_reclaim"}}), "reclaim")

    def test_material_hash_ignores_unbounded_raw_evidence(self):
        base = {"signal_key": "000001.SZ:watch:green_reclaim_research_v1", "signal_type": "watch",
                "conditions": {"price": 10.001, "volume_ratio": 2.11, "main_net_inflow": 5}}
        changed_only_raw = {**base, "evidence": {"provider_request_id": "different"}}
        self.assertEqual(material_state_hash(base), material_state_hash(changed_only_raw))
        changed_price = {**base, "conditions": {**base["conditions"], "price": 10.20}}
        self.assertNotEqual(material_state_hash(base), material_state_hash(changed_price))


if __name__ == "__main__":
    unittest.main()
