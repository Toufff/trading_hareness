import unittest
from datetime import datetime, timedelta, timezone

from app.intraday_event_replay_runner import replay_recorded_signal_events, run_recorded_signal_lifecycle_replay


UTC = timezone.utc


def event(event_id: str, observed_at: datetime, *, key: str = "000001.SZ:entry:watchlist-confirmation-v4",
          signal_type: str = "entry", state: str = "confirmed") -> dict:
    return {
        "signal_event_id": event_id,
        "symbol": "000001.SZ",
        "signal_key": key,
        "signal_type": signal_type,
        "severity": "info",
        "state": state,
        "score": 80,
        "observed_at": observed_at,
        "created_at": observed_at + timedelta(milliseconds=150),
        "conditions": {"setup": "breakout", "price": 10.0},
        "risk_flags": [],
    }


class RecordedSignalLifecycleReplayTests(unittest.TestCase):
    def test_replay_orders_by_recorded_availability_and_is_deterministic(self):
        start = datetime(2026, 8, 14, 1, 30, tzinfo=UTC)
        rows = [event("second", start + timedelta(minutes=2)), event("first", start)]
        first = replay_recorded_signal_events(rows)
        second = replay_recorded_signal_events(list(reversed(rows)))
        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual([entry["event_id"] for entry in first["trace"]], ["first", "second"])
        self.assertEqual(first["trace"][0]["output"]["lifecycle"], "started")
        self.assertEqual(first["trace"][1]["output"]["lifecycle"], "continued")
        self.assertEqual(first["data_boundary"]["provider_access"], "none")
        self.assertEqual(first["data_boundary"]["orders"], "none")

    def test_gap_rearms_and_new_session_has_a_new_lifecycle_key(self):
        start = datetime(2026, 8, 14, 1, 30, tzinfo=UTC)
        replay = replay_recorded_signal_events([
            event("one", start),
            event("two", start + timedelta(minutes=6)),
            event("three", start + timedelta(days=3)),
        ])
        self.assertEqual([entry["output"]["lifecycle"] for entry in replay["trace"]], ["started", "rearmed", "started"])
        self.assertEqual(replay["metrics"]["events"], 3)

    def test_missing_observed_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "observed_at"):
            replay_recorded_signal_events([{**event("bad", datetime.now(UTC)), "observed_at": None}])

    def test_identical_input_reuses_a_prior_persisted_run(self):
        rows = [event("one", datetime(2026, 8, 14, 1, 30, tzinfo=UTC))]

        class Result:
            def __init__(self, *, one=None, many=None):
                self.one, self.many = one, many or []
            def fetchone(self): return self.one
            def fetchall(self): return self.many

        class Connection:
            def __init__(self): self.sql = []
            def execute(self, sql, _params=()):
                self.sql.append(sql)
                if "FROM quant.intraday_signal_events" in sql:
                    return Result(many=rows)
                if "WHERE engine_version" in sql:
                    return Result(one={
                        "replay_run_id": "existing-run", "trace_hash": "abc", "metrics": {"events": 1},
                        "data_boundary": {"provider_access": "none"},
                    })
                raise AssertionError(f"unexpected query: {sql}")

        connection = Connection()
        result = run_recorded_signal_lifecycle_replay(connection, as_of_date=__import__("datetime").date(2026, 8, 14))
        self.assertTrue(result["reused"])
        self.assertEqual(result["replay_run_id"], "existing-run")
        self.assertFalse(any("INSERT INTO quant.intraday_replay_runs" in sql for sql in connection.sql))


if __name__ == "__main__":
    unittest.main()
