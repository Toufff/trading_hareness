import asyncio
import unittest
from datetime import datetime, timezone

from app.level1_snapshot_runtime import capture_level1_snapshot


class Level1SnapshotRuntimeTests(unittest.TestCase):
    def test_capture_persists_full_cross_section_as_raw_evidence(self):
        calls = []

        async def fetch():
            return ([{"symbol": "000001.SZ", "price": 10}], {"cross_sectional": True, "upstream_timestamp_ms": 1})

        async def persist(provider, capability, rows):
            calls.append((provider, capability, rows))
            return len(rows)

        async def open_session(_now):
            return True

        result = asyncio.run(capture_level1_snapshot(
            fetch_snapshot=fetch, persist=persist, session_open=open_session,
            now=datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc),
        ))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stored"], 1)
        self.assertEqual(calls[0][0:2], ("fuyao_ths", "a_share_prices_snapshot"))
        self.assertTrue(calls[0][2][0]["research_only"])

    def test_capture_fails_closed_outside_session(self):
        async def open_session(_now):
            return False

        async def fail_fetch():
            raise AssertionError("must not call provider outside session")

        async def persist(*_args):
            raise AssertionError("must not write outside session")

        result = asyncio.run(capture_level1_snapshot(
            fetch_snapshot=fail_fetch, persist=persist, session_open=open_session,
            now=datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc),
        ))
        self.assertEqual(result["status"], "outside_session")


if __name__ == "__main__":
    unittest.main()
