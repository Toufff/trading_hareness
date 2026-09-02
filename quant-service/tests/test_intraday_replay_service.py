"""Tests for the offline intraday rule-input replay module moved out of main.py."""

from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.intraday_replay_service import (
    IntradayEntryTimingChallengerDependencies,
    IntradayReplayDependencies,
    replay_recorded_intraday_rule_inputs,
    run_intraday_entry_timing_challengers,
)


class ReplayRecordedIntradayRuleInputsTests(unittest.TestCase):
    def test_delegates_to_the_recorded_rule_input_replay_runner(self):
        connection = MagicMock()
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        dependencies = IntradayReplayDependencies(
            database=database, model_version="watchlist-confirmation-v6",
            signal_rules=lambda *args: [{"signal_type": "watch"}],
        )
        payload = SimpleNamespace(as_of_date=date(2026, 8, 21), max_rows=10)

        import app.intraday_rule_input_replay_runner as runner_module
        original = runner_module.run_recorded_rule_input_replay
        calls = {}

        def fake_runner(connection_arg, *, as_of_date, max_rows, model_version, evaluate, evaluate_policy):
            calls["as_of_date"] = as_of_date
            calls["max_rows"] = max_rows
            calls["model_version"] = model_version
            return {"status": "completed"}

        import app.intraday_replay_service as module
        module.run_recorded_rule_input_replay = fake_runner
        try:
            result = replay_recorded_intraday_rule_inputs(payload, dependencies)
        finally:
            module.run_recorded_rule_input_replay = original

        self.assertEqual(result, {"status": "completed"})
        self.assertEqual(calls["as_of_date"], date(2026, 8, 21))
        self.assertEqual(calls["max_rows"], 10)
        self.assertEqual(calls["model_version"], "watchlist-confirmation-v6")


class RunIntradayEntryTimingChallengersTests(unittest.TestCase):
    def test_blocked_when_no_snapshots_exist_for_the_model_version(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = None
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        dependencies = IntradayEntryTimingChallengerDependencies(
            database=database, model_version="watchlist-confirmation-v6",
            pure_signal_rules=lambda *args, **kwargs: [], number=float, upside_assessment=lambda *args: None,
        )
        payload = SimpleNamespace(as_of_date=None, max_rows=10)

        result = run_intraday_entry_timing_challengers(payload, dependencies)

        self.assertEqual(result, {"status": "blocked", "reason": "no recorded rule-input snapshots for this model version"})

    def test_uses_the_explicit_as_of_date_without_querying_for_the_latest(self):
        connection = MagicMock()
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        dependencies = IntradayEntryTimingChallengerDependencies(
            database=database, model_version="watchlist-confirmation-v6",
            pure_signal_rules=lambda *args, **kwargs: [], number=float, upside_assessment=lambda *args: None,
        )
        payload = SimpleNamespace(as_of_date=date(2026, 8, 21), max_rows=10)

        import app.intraday_replay_service as module
        original = module.run_intraday_entry_timing_challenger_backtest
        calls = {}

        def fake_backtest(connection_arg, as_of_date, *, model_version, evaluate_variant, max_rows):
            calls["as_of_date"] = as_of_date
            return {"status": "completed"}

        module.run_intraday_entry_timing_challenger_backtest = fake_backtest
        try:
            result = run_intraday_entry_timing_challengers(payload, dependencies)
        finally:
            module.run_intraday_entry_timing_challenger_backtest = original

        self.assertEqual(result, {"status": "completed"})
        self.assertEqual(calls["as_of_date"], date(2026, 8, 21))
        connection.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
