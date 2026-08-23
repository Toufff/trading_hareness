from datetime import datetime, timezone
import unittest

from app.intraday_signal_generation import (
    IntradaySignalGenerationDependencies,
    generate_intraday_signals,
)


class IntradaySignalGenerationTests(unittest.TestCase):
    def test_keeps_base_shadow_rebound_and_eac_candidates_separate(self):
        seen: dict[str, object] = {}

        def acceptance(conditions, **kwargs):
            seen["conditions"] = conditions
            seen.update(kwargs)
            return {"status": "candidate", "score": 83.0, "risk_flags": ["needs_volume_confirmation"]}

        dependencies = IntradaySignalGenerationDependencies(
            base_rules=lambda *_: [{"signal_key": "000001.SZ:watch:base", "signal_type": "watch"}],
            shadow_signal=lambda *_: {"signal_key": "000001.SZ:watch:shadow", "signal_type": "watch"},
            rebound_signal=lambda *_: {"signal_key": "000001.SZ:entry:rebound", "signal_type": "entry"},
            rebound_failure_signal=lambda *_: None,
            eac_acceptance=acceptance,
        )
        observed_at = datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc)
        signals = generate_intraday_signals(
            watch={"symbol": "000001.SZ"}, symbol="000001.SZ",
            quote={"price": 11.2, "pct_change": 2.4, "volume_ratio": 2.1, "turnover_rate": 3.3, "main_net_inflow": 12},
            previous_quote={"price": 11.0}, daily_factors={"status": "ready"},
            minute_features={"time": "10:00"}, peer_context={"peer_count": 3},
            shadow_prior={"value": 1}, rebound_prior={"value": 2},
            first_eac={"observed_at": observed_at, "conditions": {"setup": "first_breakout"}},
            observed_at=observed_at, dependencies=dependencies,
        )

        self.assertEqual([signal["signal_key"] for signal in signals], [
            "000001.SZ:watch:base", "000001.SZ:watch:shadow", "000001.SZ:entry:rebound",
            "000001.SZ:entry:upside_acceptance_eac_v4",
        ])
        eac = signals[-1]
        self.assertEqual(eac["conditions"]["eac_state"], "candidate")
        self.assertIn("needs_volume_confirmation", eac["risk_flags"])
        self.assertEqual(seen["first_observed_at"], observed_at)
        self.assertEqual(seen["minute_features"], {"time": "10:00"})

    def test_attention_only_eac_stays_watch_and_preserves_missing_context_markers(self):
        dependencies = IntradaySignalGenerationDependencies(
            base_rules=lambda *_: [], shadow_signal=lambda *_: None,
            rebound_signal=lambda *_: None, rebound_failure_signal=lambda *_: None,
            eac_acceptance=lambda *_args, **_kwargs: {"status": "attention_only", "score": 51, "risk_flags": []},
        )
        signals = generate_intraday_signals(
            watch={"symbol": "000001.SZ"}, symbol="000001.SZ", quote=None, previous_quote=None,
            daily_factors={}, minute_features=None, peer_context=None, shadow_prior=None, rebound_prior=None,
            first_eac={"observed_at": datetime(2026, 8, 22, tzinfo=timezone.utc), "conditions": {}},
            observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc), dependencies=dependencies,
        )

        self.assertEqual(signals[0]["signal_type"], "watch")
        self.assertEqual(signals[0]["conditions"]["minute_features"], {"status": "not_available"})
        self.assertEqual(signals[0]["conditions"]["peer_context"], {"status": "not_available"})


if __name__ == "__main__":
    unittest.main()
