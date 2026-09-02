"""Tests for the per-scan xiaojie leader-pool runtime moved out of main.py."""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.xiaojie_leader_flow_runtime import (
    XiaojieLeaderFlowDependencies,
    persist_xiaojie_signal_event,
    run_xiaojie_leader_flow,
)


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class PersistXiaojieSignalEventTests(unittest.TestCase):
    def test_inserts_one_research_stage_row_and_returns_its_id(self):
        connection = MagicMock()
        scan_id = uuid.uuid4()
        observed_at = datetime(2026, 8, 27, 9, 40, tzinfo=timezone.utc)
        candidate = {"symbol": "600176.SH", "mode": "ma5_break", "position": {}, "stop_loss": {},
                     "evidence": {}, "risk_flags": []}

        event_id = persist_xiaojie_signal_event(connection, scan_id, observed_at, candidate)

        self.assertIsInstance(event_id, uuid.UUID)
        connection.execute.assert_called_once()
        sql, params = connection.execute.call_args.args
        self.assertIn("xiaojie_leader_flow_research", sql)
        self.assertEqual(params[0], event_id)
        self.assertEqual(params[2], "600176.SH")


class RunXiaojieLeaderFlowTests(unittest.TestCase):
    def test_returns_skipped_status_when_the_cross_section_is_empty(self):
        dependencies = XiaojieLeaderFlowDependencies(
            database=MagicMock(), run_database=None, session_context=None,
            ma5_break_state={}, velocity_state={}, deliver_alert=None,
            alert_text=None, safe_error_detail=None,
        )

        result = _run(run_xiaojie_leader_flow(
            scan_id=uuid.uuid4(), observed_at=datetime.now(timezone.utc), all_a_rows=[],
            dependencies=dependencies,
        ))

        self.assertEqual(result, {"status": "skipped", "reason": "no all-A cross-section in this scan"})

    def test_returns_blocked_status_when_session_limits_are_unavailable(self):
        async def session_context(_trading_date):
            return {"limits": {}}

        dependencies = XiaojieLeaderFlowDependencies(
            database=MagicMock(), run_database=None, session_context=session_context,
            ma5_break_state={}, velocity_state={}, deliver_alert=None,
            alert_text=None, safe_error_detail=None,
        )

        result = _run(run_xiaojie_leader_flow(
            scan_id=uuid.uuid4(), observed_at=datetime.now(timezone.utc), all_a_rows=[{"symbol": "600176.SH"}],
            dependencies=dependencies,
        ))

        self.assertEqual(result, {"status": "blocked", "reason": "session trade limits unavailable"})


if __name__ == "__main__":
    unittest.main()
