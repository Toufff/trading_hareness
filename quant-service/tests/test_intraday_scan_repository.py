from __future__ import annotations

from datetime import date, datetime, timezone
import unittest
from unittest.mock import MagicMock

from app.intraday_scan_repository import (
    first_eac_breakout_events,
    load_intraday_scan_local_state,
    load_intraday_signal_event_state,
    previous_quote_frames,
)
from app.watchlist_daily_factors import watchlist_daily_factors_by_symbol


class IntradayScanRepositoryTests(unittest.TestCase):
    def test_previous_quotes_are_loaded_once_per_symbol_source_pair(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [
            {"symbol": "000001.SZ", "source_name": "tencent_free", "price": 10.0,
             "observed_at": datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)},
        ]
        frames = previous_quote_frames(
            connection,
            {"000001.SZ": "tencent_free", "600000.SH": "sina_free"},
            not_before=datetime(2026, 8, 17, 0, 59, 45, tzinfo=timezone.utc),
            observed_at=datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(frames["000001.SZ"]["price"], 10.0)
        self.assertEqual(connection.execute.call_count, 1)
        sql, params = connection.execute.call_args.args
        self.assertIn("DISTINCT ON(o.symbol,o.source_name)", sql)
        self.assertEqual(params[0], ["000001.SZ", "600000.SH"])
        self.assertEqual(params[1], ["tencent_free", "sina_free"])

    def test_first_eac_events_are_batched_and_use_earliest_event(self) -> None:
        connection = MagicMock()
        first_at = datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)
        connection.execute.return_value.fetchall.return_value = [
            {"symbol": "000001.SZ", "observed_at": first_at, "conditions": {"setup": "eac"}},
        ]
        events = first_eac_breakout_events(
            connection, ["000001.SZ", "000001.SZ", "600000.SH"],
            not_before=datetime(2026, 8, 17, 0, 55, tzinfo=timezone.utc),
        )
        self.assertEqual(events["000001.SZ"]["observed_at"], first_at)
        self.assertEqual(connection.execute.call_count, 1)
        sql, params = connection.execute.call_args.args
        self.assertIn("DISTINCT ON(symbol)", sql)
        self.assertIn("signal_key=symbol || ':watch:upside_breakout_eac_v3'", sql)
        self.assertEqual(params[0], ["000001.SZ", "600000.SH"])

    def test_empty_batches_do_not_query_database(self) -> None:
        connection = MagicMock()
        now = datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(previous_quote_frames(connection, {}, not_before=now, observed_at=now), {})
        self.assertEqual(first_eac_breakout_events(connection, [], not_before=now), {})
        connection.execute.assert_not_called()

    def test_scan_local_state_keeps_bounded_local_reads_and_point_in_time_membership(self) -> None:
        class Result:
            def __init__(self, *, rows=None, row=None):
                self.rows, self.row = rows or [], row

            def fetchall(self):
                return self.rows

            def fetchone(self):
                return self.row

        class Connection:
            def __init__(self):
                self.calls = []
                self.results = iter([
                    Result(rows=[{"symbol": "000001.SZ", "observed_at": datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc), "raw": {}}]),
                    Result(rows=[{"symbol": "000001.SZ", "quantity": 100, "sellable_quantity": 0, "average_cost": 10.0}]),
                    Result(rows=[{"symbol": "000001.SZ", "sector_key": "pcb"}]),
                    Result(row={"drawdown": -0.02, "payload": {"equity": 99.0}}),
                ])

            def execute(self, sql, params=None):
                self.calls.append((str(sql), params))
                return next(self.results)

        observed_at = datetime(2026, 8, 17, 1, 5, tzinfo=timezone.utc)
        connection = Connection()
        state = load_intraday_scan_local_state(
            connection, ["000001.SZ"], observed_at=observed_at,
            session_start=datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc),
            local_trade_date=date(2026, 8, 17),
        )
        self.assertEqual(len(connection.calls), 4)
        self.assertEqual(state.order_book_by_symbol["000001.SZ"][0]["raw"], {})
        self.assertEqual(state.paper_positions["000001.SZ"]["sellable_quantity"], 0)
        self.assertEqual(state.candidate_sector_keys, {"000001.SZ": ["pcb"]})
        self.assertEqual(state.snapshot_payload, {"equity": 99.0, "drawdown": -0.02})
        order_book_sql, order_book_params = connection.calls[0]
        self.assertIn("source_name='tencent_order_book'", order_book_sql)
        self.assertEqual(order_book_params[1], datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc))
        membership_sql, membership_params = connection.calls[2]
        self.assertIn("effective_from<=", membership_sql)
        self.assertEqual(membership_params[1:], (date(2026, 8, 17), date(2026, 8, 17), date(2026, 8, 17)))

    def test_signal_state_batches_per_key_reads_and_preserves_alert_payload(self) -> None:
        class Result:
            def __init__(self, *, rows=None, row=None):
                self.rows, self.row = rows or [], row

            def fetchall(self):
                return self.rows

            def fetchone(self):
                return self.row

        class Connection:
            def __init__(self):
                self.calls = []
                at = datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)
                self.results = iter([
                    Result(rows=[{"signal_key": "000001.SZ:watch:one", "observed_at": at}]),
                    Result(rows=[{"signal_key": "000001.SZ:watch:one", "observed_at": at, "score": 80, "conditions": {"price": 10.0}}]),
                    Result(row={"observed_at": at}),
                ])

            def execute(self, sql, params=None):
                self.calls.append((str(sql), params))
                return next(self.results)

        connection = Connection()
        state = load_intraday_signal_event_state(
            connection, ["000001.SZ:watch:one", "000001.SZ:watch:one", "000001.SZ:watch:two"],
            "000001.SZ", session_start=datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(connection.calls), 3)
        self.assertEqual(sorted(state.latest_by_key), ["000001.SZ:watch:one"])
        self.assertEqual(state.last_alerted_by_key["000001.SZ:watch:one"]["conditions"]["price"], 10.0)
        self.assertEqual(state.last_symbol_watch_alerted["observed_at"], datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(connection.calls[0][1], (["000001.SZ:watch:one", "000001.SZ:watch:two"],))
        self.assertIn("DISTINCT ON(signal_key)", connection.calls[0][0])
        self.assertIn("state='alerted'", connection.calls[1][0])

    def test_empty_signal_state_does_not_query_database(self) -> None:
        connection = MagicMock()
        state = load_intraday_signal_event_state(
            connection, [], "000001.SZ", session_start=datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(state.latest_by_key, {})
        self.assertIsNone(state.last_symbol_watch_alerted)
        connection.execute.assert_not_called()

    def test_daily_factors_for_watch_basket_use_one_ranked_query(self) -> None:
        rows = []
        for symbol, offset in (("000001.SZ", 0.0), ("600000.SH", 10.0)):
            for day in range(1, 26):
                close = offset + 10.0 + day / 10
                rows.append({
                    "symbol": symbol, "trading_date": date(2026, 7, day), "high": close * 1.02,
                    "low": close * 0.98, "close": close, "volume": 1000 + day, "adj_factor": 1.0,
                    "is_suspended": False, "limit_up": close * 1.1, "limit_down": close * 0.9, "is_st": False,
                })
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = rows
        factors = watchlist_daily_factors_by_symbol(
            ["000001.SZ", "600000.SH"], connection,
            number=lambda value: float(value) if value is not None else None,
        )
        self.assertEqual(connection.execute.call_count, 1)
        self.assertEqual(factors["000001.SZ"]["status"], "completed")
        self.assertEqual(factors["600000.SH"]["status"], "completed")
        self.assertGreater(factors["600000.SH"]["latest_daily_close"], factors["000001.SZ"]["latest_daily_close"])
        self.assertIn("row_number() OVER(PARTITION BY b.symbol", connection.execute.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
