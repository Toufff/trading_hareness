from __future__ import annotations

from datetime import date
import unittest
from unittest.mock import MagicMock

from app.intraday_clock import minute_bucket
from app.intraday_volume_profiles import attach_volume_time_profile, volume_time_profiles


class IntradayVolumeProfileTests(unittest.TestCase):
    def test_batch_profiles_keep_prior_day_minimum_sample_gate(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [
            {"symbol": "000001.SZ", "minute_bucket": "10:00", "sample_days": 8, "median_volume": 200},
            {"symbol": "600000.SH", "minute_bucket": "10:01", "sample_days": 7, "median_volume": 300},
        ]
        profiles = volume_time_profiles(
            {"000001.SZ": "2026-08-17 10:00:00", "600000.SH": "10:01", "300001.SZ": "bad"},
            date(2026, 8, 17), connection,
            minute_bucket_fn=minute_bucket,
            number=lambda value: float(value) if value is not None else None,
        )
        self.assertEqual(connection.execute.call_count, 1)
        self.assertEqual(profiles["000001.SZ"]["status"], "ready")
        self.assertEqual(profiles["600000.SH"]["status"], "insufficient_history")
        self.assertEqual(profiles["300001.SZ"]["status"], "invalid_minute_bucket")
        self.assertIn("JOIN unnest(%s::text[],%s::text[])", connection.execute.call_args.args[0])

    def test_attach_preserves_feature_and_computes_surprise_only_when_ready(self) -> None:
        feature = {"time": "10:00", "minute_volume_lot": 600, "return_3m_pct": 1.2}
        attached = attach_volume_time_profile(
            feature, {"status": "ready", "median_volume": 200, "sample_days": 8},
            number=lambda value: float(value) if value is not None else None,
        )
        self.assertEqual(attached["return_3m_pct"], 1.2)
        self.assertEqual(attached["time_bucket_volume_profile"]["volume_surprise"], 3.0)
        insufficient = attach_volume_time_profile(
            feature, {"status": "insufficient_history", "median_volume": 200, "sample_days": 7},
            number=lambda value: float(value) if value is not None else None,
        )
        self.assertIsNone(insufficient["time_bucket_volume_profile"]["volume_surprise"])


if __name__ == "__main__":
    unittest.main()
