from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock

from app.ten_day_leader_rotation_intraday_repository import persist_intraday_rotation_observations


class TenDayLeaderRotationIntradayRepositoryTests(unittest.TestCase):
    def test_persists_research_only_observation_with_scan_idempotency(self) -> None:
        connection = MagicMock()
        count = persist_intraday_rotation_observations(
            connection, run_id="run-1", scan_id="scan-1", json_safe=lambda value: value,
            observations=[{
                "symbol": "600001.SH", "observed_at": datetime(2026, 8, 24, 2, tzinfo=timezone.utc),
                "quote_source": "tencent_all_a_snapshot", "shadow_state": "confirmed_coordination",
                "shadow_eligible": True, "decision_eligible": False, "evidence": {},
                "reason_codes": ["ten_day_rank"], "risk_flags": ["shadow_sample_only"], "source_snapshot": {},
            }],
        )

        self.assertEqual(count, 1)
        sql = connection.execute.call_args.args[0]
        self.assertIn("ten_day_leader_rotation_intraday_observations", sql)
        self.assertIn("ON CONFLICT(run_id,scan_id,symbol)", sql)
        self.assertIn("false", sql)


if __name__ == "__main__":
    unittest.main()
