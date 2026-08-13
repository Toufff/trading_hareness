from datetime import datetime, timezone, timedelta
import unittest

from app.episode_lifecycle import material_state_hash, material_state_payload, signal_stage, strategy_family


class EpisodeLifecycleTests(unittest.TestCase):
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
