from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import date, datetime, timezone
import unittest

from app.strategy_runtime_runners import (
    PostCloseStrategyRuntimeDependencies,
    StrategyReviewRuntimeDependencies,
    persist_strategy_review,
    run_post_close_strategy_loop,
    run_strategy_review_loop,
)


class _Database:
    @contextmanager
    def transaction(self):
        yield object()


class StrategyRuntimeRunnerTests(unittest.TestCase):
    def test_persist_review_uses_one_shared_transaction_boundary(self) -> None:
        calls = []

        def payload(connection, request):
            calls.append((connection, request))
            return {"status": "completed"}

        request = {"session": "close"}
        result = persist_strategy_review(_Database(), payload, request)

        self.assertEqual(result, {"status": "completed"})
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][1], request)

    def test_review_runner_preserves_checkpoint_operations_and_timeouts(self) -> None:
        calls: list[tuple[str, object]] = []
        exchange_date = date(2026, 8, 21)

        async def run_database(operation, *args, timeout_seconds=None):
            calls.append(("database", timeout_seconds))
            return operation(*args)

        async def scheduler(schedule):
            snapshot = await schedule.build_market_snapshot(exchange_date, "close")
            self.assertEqual(snapshot["session"], "close")
            await schedule.build_board_report()
            await schedule.recompute_outcomes(exchange_date)
            await schedule.recompute_analyst_intraday_outcomes(exchange_date)
            await schedule.recompute_scorecards(exchange_date)
            await schedule.build_analyst_market_review("daily", exchange_date)
            await schedule.persist_review(exchange_date, "close")
            self.assertTrue(await schedule.completed_for_checkpoint(exchange_date, "close"))

        async def snapshot(payload):
            calls.append(("snapshot", payload))
            return {"session": payload["session"]}

        async def board_report(*, deliver):
            calls.append(("board", deliver))
            return {"status": "completed"}

        async def calendar_open(_):
            return True

        async def sync_index(_):
            return None

        def review_payload(connection, payload):
            self.assertIsNotNone(connection)
            calls.append(("review", payload))
            return {"status": "completed"}

        def completed(connection, received_date, session):
            self.assertIsNotNone(connection)
            calls.append(("completed", (received_date, session)))
            return True

        asyncio.run(run_strategy_review_loop(StrategyReviewRuntimeDependencies(
            database=_Database(), run_database=run_database, calendar_open=calendar_open,
            sync_index_context=sync_index, build_market_snapshot=snapshot,
            market_snapshot_request=lambda session: {"session": session, "refresh_public_quotes": True},
            build_board_report=board_report,
            recompute_outcomes=lambda received_date: {"outcomes": str(received_date)},
            recompute_analyst_intraday_outcomes=lambda received_date: {"analyst": str(received_date)},
            recompute_scorecards=lambda received_date: {"scorecards": str(received_date)},
            strategy_review_payload=review_payload,
            strategy_review_request=lambda **payload: payload,
            completed_for_checkpoint=completed,
            build_analyst_market_review=lambda _database, cadence, received_date: {
                "cadence": cadence, "date": str(received_date),
            },
            now=lambda: datetime(2026, 8, 21, 15, 5, tzinfo=timezone.utc), scheduler=scheduler,
        )))
        self.assertIn(("snapshot", {"session": "close", "refresh_public_quotes": True}), calls)
        self.assertIn(("board", False), calls)
        self.assertIn(("review", {"session": "close", "as_of_date": exchange_date, "persist": True}), calls)
        self.assertIn(("completed", (exchange_date, "close")), calls)
        self.assertEqual([item[1] for item in calls if item[0] == "database"], [60, 90, 30, 90, 30, 10])

    def test_post_close_runner_preserves_same_date_receipts_and_operations(self) -> None:
        calls: list[tuple[str, object]] = []
        exchange_date = date(2026, 8, 21)

        async def run_database(operation, *args, timeout_seconds=None):
            calls.append(("database", timeout_seconds))
            return operation(*args)

        async def scheduler(schedule):
            self.assertEqual(await schedule.completed_for_date(exchange_date), (False, False))
            self.assertEqual(await schedule.run_strategy(exchange_date), "partial")
            self.assertEqual(await schedule.run_main_wave(exchange_date), "completed")

        async def calendar_open(_):
            return True

        def run_recorded(_database, **kwargs):
            calls.append(("recorded", kwargs["task_key"]))
            kwargs["operation"]()
            return {"status": "partial" if kwargs["task_key"] == "post_close_strategy" else "completed"}

        def post_close(payload):
            calls.append(("post_close", payload))
            return {"status": "completed"}

        def main_wave(payload):
            calls.append(("main_wave", payload))
            return {"status": "completed"}

        asyncio.run(run_post_close_strategy_loop(PostCloseStrategyRuntimeDependencies(
            database=_Database(), run_database=run_database, calendar_open=calendar_open,
            retry_window=lambda _: True, strategy_completed_for_date=lambda _: False,
            main_wave_completed_for_date=lambda _: False, run_recorded=run_recorded,
            run_post_close_strategy=post_close, post_close_request=lambda **payload: payload,
            post_close_model_version="post-close-v1", run_main_wave_research=main_wave,
            main_wave_request=lambda **payload: payload,
            now=lambda: datetime(2026, 8, 21, 19, 0, tzinfo=timezone.utc), scheduler=scheduler,
        )))
        self.assertEqual([item for item in calls if item[0] == "recorded"], [
            ("recorded", "post_close_strategy"), ("recorded", "watchlist_main_wave"),
        ])
        self.assertIn(("post_close", {"as_of_date": exchange_date}), calls)
        self.assertIn(("main_wave", {"as_of_date": exchange_date}), calls)
        self.assertEqual([item[1] for item in calls if item[0] == "database"], [10, 10, 60, 90])

    def test_post_close_runner_shares_one_in_flight_set_across_both_recorded_calls(self) -> None:
        """WP6: a process-local in-flight set guards run_strategy/run_main_wave.

        Both calls must receive the *same* set object so a concurrent
        same-process retry of either run_key can be detected, and the set
        must end up empty again once both calls have completed normally.
        """
        exchange_date = date(2026, 8, 21)
        seen_in_flight_sets: list[set] = []

        async def run_database(operation, *args, timeout_seconds=None):
            return operation(*args)

        async def scheduler(schedule):
            await schedule.completed_for_date(exchange_date)
            await schedule.run_strategy(exchange_date)
            await schedule.run_main_wave(exchange_date)

        async def calendar_open(_):
            return True

        def run_recorded(_database, **kwargs):
            # The real automation_run_repository.run_recorded owns adding to
            # and discarding from this set; this fake only needs to confirm
            # the loop passes it through, and passes the *same* instance to
            # every call so a same-process race is detectable.
            seen_in_flight_sets.append(kwargs["in_flight_run_keys"])
            kwargs["operation"]()
            return {"status": "completed"}

        asyncio.run(run_post_close_strategy_loop(PostCloseStrategyRuntimeDependencies(
            database=_Database(), run_database=run_database, calendar_open=calendar_open,
            retry_window=lambda _: True, strategy_completed_for_date=lambda _: False,
            main_wave_completed_for_date=lambda _: False, run_recorded=run_recorded,
            run_post_close_strategy=lambda payload: {"status": "completed"},
            post_close_request=lambda **payload: payload, post_close_model_version="post-close-v1",
            run_main_wave_research=lambda payload: {"status": "completed"},
            main_wave_request=lambda **payload: payload,
            now=lambda: datetime(2026, 8, 21, 19, 0, tzinfo=timezone.utc), scheduler=scheduler,
        )))
        self.assertEqual(len(seen_in_flight_sets), 2)
        self.assertIs(seen_in_flight_sets[0], seen_in_flight_sets[1])


if __name__ == "__main__":
    unittest.main()
