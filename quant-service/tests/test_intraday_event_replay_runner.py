import unittest
from datetime import datetime, timedelta, timezone

from app.intraday_event_replay_runner import replay_recorded_signal_events, run_recorded_signal_lifecycle_replay
from app.intraday_rule_input_replay_runner import replay_recorded_rule_inputs, run_recorded_rule_input_replay
from app.intraday_rule_inputs import intraday_rule_input_hash, intraday_rule_input_payload


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


def rule_input(snapshot_id: str, observed_at: datetime, *, symbol: str = "000001.SZ") -> dict:
    payload = intraday_rule_input_payload(
        watch={"symbol": symbol, "alert_on_entry": True, "alert_on_exit": True, "metadata": {}},
        quote={"symbol": symbol, "price": 10.3, "pct_change": 3.0, "volume_ratio": 2.0,
               "turnover_rate": 3.0, "main_net_inflow": 100.0},
        previous_quote={"symbol": symbol, "source_name": "tencent_free", "price": 10.0},
        daily_factors={"status": "completed"}, minute_features={"status": "not_available"},
        peer_context={"status": "not_available"}, model_version="watchlist-confirmation-v4",
    )
    return {
        "rule_input_snapshot_id": snapshot_id, "scan_id": "scan-1", "symbol": symbol,
        "observed_at": observed_at, "model_version": "watchlist-confirmation-v4",
        "input_hash": intraday_rule_input_hash(payload), "inputs": payload,
        "created_at": observed_at + timedelta(milliseconds=100),
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


class RecordedRuleInputReplayTests(unittest.TestCase):
    def test_replays_frozen_non_signal_inputs_in_availability_order(self):
        start = datetime(2026, 8, 14, 1, 30, tzinfo=UTC)
        rows = [rule_input("second", start + timedelta(minutes=1), symbol="000002.SZ"),
                rule_input("first", start, symbol="000001.SZ")]

        def evaluate(inputs):
            return [{"signal_key": f"{inputs['watch']['symbol']}:watch:replayed", "signal_type": "watch",
                     "severity": "info", "score": 1, "conditions": {"source": "frozen"}, "risk_flags": []}]

        first = replay_recorded_rule_inputs(rows, evaluate=evaluate, expected_model_version="watchlist-confirmation-v4")
        second = replay_recorded_rule_inputs(list(reversed(rows)), evaluate=evaluate,
                                              expected_model_version="watchlist-confirmation-v4")
        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual([entry["event_id"] for entry in first["trace"]], ["first", "second"])
        self.assertEqual(first["metrics"]["snapshots"], 2)
        self.assertEqual(first["metrics"]["emitted_signals"], 2)
        self.assertEqual(first["data_boundary"]["provider_access"], "none")
        self.assertEqual(first["metrics"]["policy_replayable_snapshots"], 2)
        self.assertEqual(first["metrics"]["policy_evaluated_signals"], 0)
        self.assertIn("policy/risk gate", first["data_boundary"]["interpretation"])

    def test_v2_snapshot_replays_frozen_policy_gate_without_database_state(self):
        start = datetime(2026, 8, 14, 1, 30, tzinfo=UTC)
        row = rule_input("one", start)
        policy_calls = []

        def evaluate(_inputs):
            return [{"signal_key": "000001.SZ:entry:replayed", "signal_type": "entry", "severity": "warning",
                     "score": 80, "conditions": {}, "risk_flags": []}]

        def evaluate_policy(signal, inputs):
            policy_calls.append((signal["signal_type"], inputs["portfolio_context"]))
            return {"version": "live-policy-gate-v1", "decision": "watch_only", "allow_confirmation": False,
                    "reason_codes": ["market_context_missing"], "risk_flags": ["policy_market_context_missing"],
                    "market_state": "unknown", "price_limit_state": {}, "available_quantity": 0}

        replay = replay_recorded_rule_inputs(
            [row], evaluate=evaluate, evaluate_policy=evaluate_policy,
            expected_model_version="watchlist-confirmation-v4",
        )

        self.assertEqual(policy_calls, [("entry", {"position": {}, "snapshot": {}, "candidate_sector_keys": []})])
        self.assertEqual(replay["metrics"]["policy_evaluated_signals"], 1)
        self.assertEqual(replay["metrics"]["policy_blocked_signals"], 1)
        self.assertFalse(replay["trace"][0]["output"]["signals"][0]["policy_gate"]["allow_confirmation"])

    def test_rejects_tampered_input_hash_and_reuses_persisted_result(self):
        start = datetime(2026, 8, 14, 1, 30, tzinfo=UTC)
        tampered = rule_input("bad", start)
        tampered["input_hash"] = "not-the-payload-hash"
        with self.assertRaisesRegex(ValueError, "mismatched input_hash"):
            replay_recorded_rule_inputs([tampered], evaluate=lambda _inputs: [],
                                        expected_model_version="watchlist-confirmation-v4")

        rows = [rule_input("one", start)]

        class Result:
            def __init__(self, *, one=None, many=None): self.one, self.many = one, many or []
            def fetchone(self): return self.one
            def fetchall(self): return self.many

        class Connection:
            def __init__(self): self.sql = []
            def execute(self, sql, _params=()):
                self.sql.append(sql)
                if "FROM quant.intraday_rule_input_snapshots" in sql:
                    return Result(many=rows)
                if "WHERE engine_version" in sql:
                    return Result(one={"replay_run_id": "existing-run", "trace_hash": "abc",
                                       "metrics": {"snapshots": 1}, "data_boundary": {"provider_access": "none"}})
                raise AssertionError(f"unexpected query: {sql}")

        result = run_recorded_rule_input_replay(
            Connection(), as_of_date=__import__("datetime").date(2026, 8, 14), max_rows=50_000,
            model_version="watchlist-confirmation-v4", evaluate=lambda _inputs: [],
        )
        self.assertTrue(result["reused"])
        self.assertEqual(result["replay_run_id"], "existing-run")


if __name__ == "__main__":
    unittest.main()
