"""The deploy-window gate and the deployment announcement channel.

Both exist because of one measurement (2026-09-20, three publishes): the owner
HTTP surface is down 9.3 s per release, the shared tunnel 5-7 s on two of the
three, and PostgreSQL not at all.  Fifteen seconds is survivable by a consumer
that knows what it is looking at, and a lost collection cycle by one that does
not -- so the policy is about *when* a release runs and *whether it is
announced*, never about making it faster.
"""

from __future__ import annotations

import unittest
from datetime import datetime

from app.deploy_window import SHANGHAI, evaluate
from app.owner_deploy_events import (
    SURFACE_HTTP_API,
    SURFACE_SHARED_TUNNEL,
    RecordResult,
    expected_seconds,
    in_progress,
    latest,
    new_deploy_id,
    record,
)
from app.peer_contract import DEPLOY_CHANNEL, SUPPORTED_OBJECTS


def moment(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=SHANGHAI)


class DeployWindowTests(unittest.TestCase):
    # 2026-09-21 is a Monday and an exchange trading day; 2026-09-19 a Saturday;
    # 2026-10-01 falls inside the National Day closure in calendar_2026.json.
    def test_the_session_is_closed_to_releases(self):
        decision = evaluate(moment("2026-09-21T10:30:00"))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "trading_session")
        self.assertTrue(decision.calendar_verified)

    def test_the_session_window_opens_before_the_bell_and_closes_after_it(self):
        # 09:15 is already the call auction, and 15:00-15:10 is the post-close drain.
        self.assertFalse(evaluate(moment("2026-09-21T09:15:00")).allowed)
        self.assertFalse(evaluate(moment("2026-09-21T15:09:59")).allowed)
        self.assertTrue(evaluate(moment("2026-09-21T09:14:59")).allowed)
        self.assertTrue(evaluate(moment("2026-09-21T15:10:00")).allowed)

    def test_the_peer_batch_window_is_closed_even_though_the_market_is_shut(self):
        decision = evaluate(moment("2026-09-21T07:00:00"))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "peer_batch_window")

    def test_a_weekend_is_open_all_day_outside_the_batch_window(self):
        self.assertTrue(evaluate(moment("2026-09-19T10:30:00")).allowed)
        # The batch window is weekday-only: the consumer's timer is Mon..Fri.
        self.assertTrue(evaluate(moment("2026-09-19T07:00:00")).allowed)

    def test_an_exchange_holiday_inside_session_hours_is_open(self):
        decision = evaluate(moment("2026-10-01T10:30:00"))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "非交易日")

    def test_an_unverified_year_closes_the_window_rather_than_opening_it(self):
        # No calendar_2031.json ships, and 2031-09-22 is a Monday. Guessing
        # "open" here would publish into a possible session; guessing "closed"
        # only costs an explicit override.
        decision = evaluate(moment("2031-09-22T10:30:00"))
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.calendar_verified)
        self.assertIn("未核验", decision.reason)


class _FakeConnection:
    def __init__(self, rows, fail=False):
        self._rows = rows
        self.fail = fail
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if self.fail:
            raise RuntimeError("connection is closed")
        return self

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return list(self._rows)


class _FakeDatabase:
    def __init__(self, connection):
        self.connection = connection

    def transaction(self):
        connection = self.connection

        class _Scope:
            def __enter__(self):
                return connection

            def __exit__(self, *exc):
                return False

        return _Scope()


class AnnouncementTests(unittest.TestCase):
    def test_the_expected_window_follows_the_surfaces_actually_touched(self):
        self.assertEqual(expected_seconds({SURFACE_HTTP_API: True, SURFACE_SHARED_TUNNEL: False}), 10)
        self.assertEqual(expected_seconds({SURFACE_HTTP_API: True, SURFACE_SHARED_TUNNEL: True}), 17)
        self.assertEqual(expected_seconds({}), 0)

    def test_a_phase_is_appended_with_its_surfaces(self):
        connection = _FakeConnection([{"event_id": 7, "recorded_at": moment("2026-09-20T11:37:50")}])
        result = record(
            _FakeDatabase(connection),
            deploy_id="deploy-x", phase="starting", release_id="rel-1", git_sha="abc123",
            surfaces={SURFACE_HTTP_API: True, SURFACE_SHARED_TUNNEL: False},
        )
        self.assertTrue(result.recorded)
        self.assertEqual(result.event_id, 7)
        self.assertIn("owner_deploy_events", connection.statements[0])

    def test_a_database_failure_never_propagates_out_of_the_announcement(self):
        # A release blocked by its own bookkeeping is worse than an
        # unannounced one, so this has to be a result, not an exception.
        result = record(
            _FakeDatabase(_FakeConnection([], fail=True)),
            deploy_id="deploy-x", phase="starting", release_id="rel-1",
        )
        self.assertIsInstance(result, RecordResult)
        self.assertFalse(result.recorded)
        self.assertIn("RuntimeError", result.error)

    def test_an_unknown_phase_is_refused_without_touching_the_database(self):
        connection = _FakeConnection([])
        result = record(_FakeDatabase(connection), deploy_id="d", phase="rollback", release_id="r")
        self.assertFalse(result.recorded)
        self.assertEqual(connection.statements, [])

    def test_a_deploy_with_no_terminal_row_reads_as_in_progress(self):
        rows = [
            {"deploy_id": "d2", "phase": "starting", "release_id": "r2", "git_sha": None,
             "surfaces": {SURFACE_SHARED_TUNNEL: True}, "expected_seconds": 17, "note": None,
             "recorded_at": moment("2026-09-20T12:00:00")},
            {"deploy_id": "d1", "phase": "completed", "release_id": "r1", "git_sha": None,
             "surfaces": {}, "expected_seconds": None, "note": None,
             "recorded_at": moment("2026-09-20T11:38:00")},
        ]
        items = latest(_FakeConnection(rows), limit=10)
        current = in_progress(items)
        self.assertIsNotNone(current)
        self.assertEqual(current["deploy_id"], "d2")
        self.assertTrue(current["surfaces"][SURFACE_SHARED_TUNNEL])

    def test_a_closed_deploy_is_not_reported_as_in_progress(self):
        rows = [
            {"deploy_id": "d1", "phase": "completed", "release_id": "r1", "git_sha": None,
             "surfaces": {}, "expected_seconds": None, "note": None,
             "recorded_at": moment("2026-09-20T11:38:00")},
            {"deploy_id": "d1", "phase": "starting", "release_id": "r1", "git_sha": None,
             "surfaces": {}, "expected_seconds": 10, "note": None,
             "recorded_at": moment("2026-09-20T11:37:50")},
        ]
        self.assertIsNone(in_progress(latest(_FakeConnection(rows), limit=10)))

    def test_deploy_ids_are_sortable_and_carry_their_instant(self):
        self.assertTrue(new_deploy_id(moment("2026-09-20T11:37:50")).startswith("deploy-20260920T"))


class ContractPublishesTheChannelTests(unittest.TestCase):
    def test_the_announcement_table_is_part_of_the_published_contract(self):
        names = {item.name for item in SUPPORTED_OBJECTS}
        self.assertIn("owner_deploy_events", names)

    def test_the_channel_names_both_surfaces_and_the_closed_windows(self):
        self.assertEqual(DEPLOY_CHANNEL["table"], "quant.owner_deploy_events")
        self.assertEqual(set(DEPLOY_CHANNEL["surfaces"]), {SURFACE_HTTP_API, SURFACE_SHARED_TUNNEL})
        self.assertIn("trading_session", DEPLOY_CHANNEL["windows_closed_to_releases"])
        self.assertIn("peer_batch_window", DEPLOY_CHANNEL["windows_closed_to_releases"])


if __name__ == "__main__":
    unittest.main()
