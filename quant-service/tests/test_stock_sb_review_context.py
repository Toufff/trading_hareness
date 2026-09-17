"""Review context may only expose what was observable before the selected order."""

from datetime import date, datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from app import stock_sb_review_context as context
from app.stock_sb_review import collect_review


CN = ZoneInfo("Asia/Shanghai")


def at(clock: str, day: date = date(2026, 9, 16)) -> datetime:
    hour, minute, second = (int(part) for part in clock.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=CN)


def fill(key: str, clock: str, side: str, quantity: float, price: float) -> dict:
    moment = at(clock)
    return {"event_key": key, "order_at": moment, "fill_at": moment, "side": side,
            "filled_quantity": quantity, "fill_price": price}


def snapshot(observed_at: datetime, quantity: float | None, total: float = 100000.0,
             verification: str = "verified_exact") -> dict:
    return {"observed_at": observed_at, "source": "ths_desktop_ui", "verification": verification,
            "total_asset": total, "cash": 1000, "total_market_value": None, "quantity": quantity,
            "sellable_quantity": quantity, "average_cost": 7.21 if quantity else None,
            "market_price": 7.12 if quantity else None, "market_value": None}


class PositionPathTests(unittest.TestCase):
    def test_baseline_plus_fills_reconciles_with_intraday_snapshot(self) -> None:
        events = [fill("s1", "09:38:31", "sell", 2000, 7.28), fill("s2", "10:18:06", "sell", 2000, 7.47),
                  fill("b1", "15:00:00", "buy", 300, 7.36)]
        snapshots = [snapshot(at("18:26:03", date(2026, 9, 15)), 6000),
                     snapshot(at("11:55:42"), 2000)]
        path = context.build_position_path(snapshots, events, previous_trading_day=date(2026, 9, 15))
        self.assertEqual(path["status"], "verified")
        self.assertEqual(path["events"]["s1"]["before_quantity"], 6000)
        self.assertEqual(path["events"]["b1"]["after_quantity"], 2300)
        self.assertEqual(path["events"]["s1"]["before_weight_pct_estimate"], 43.68)
        self.assertEqual(path["reconciliation"], [{"observed_at": at("11:55:42"), "snapshot_quantity": 2000,
                                                   "inferred_quantity": 2000, "match": True}])

    def test_snapshot_before_prior_close_is_stale_and_mismatch_is_visible(self) -> None:
        events = [fill("s1", "09:38:31", "sell", 2000, 7.28)]
        snapshots = [snapshot(at("11:42:33", date(2026, 9, 15)), 4000), snapshot(at("11:55:42"), 1000)]
        path = context.build_position_path(snapshots, events, previous_trading_day=date(2026, 9, 15))
        self.assertEqual(path["status"], "stale")
        self.assertFalse(path["reconciliation"][0]["match"])

    def test_missing_symbol_in_exact_snapshot_is_zero_but_unverified_is_unknown(self) -> None:
        self.assertEqual(context.normalize_account_snapshot(snapshot(at("09:00:00"), None))["quantity"], 0.0)
        partial = context.normalize_account_snapshot(snapshot(at("09:00:00"), None, verification="partial"))
        self.assertIsNone(partial["quantity"])
        path = context.build_position_path([snapshot(at("09:00:00"), None, verification="partial")],
                                           [fill("s1", "09:38:31", "sell", 100, 7)],
                                           previous_trading_day=date(2026, 9, 15))
        self.assertEqual(path["status"], "missing")


class PlanTests(unittest.TestCase):
    def test_plan_written_after_order_is_not_active(self) -> None:
        plans = context.dedupe_plans([
            {"plan_key": "old", "content_hash": "a", "created_at": at("19:54:06", date(2026, 9, 15)),
             "valid_until": at("15:00:00")},
            {"plan_key": "dup", "content_hash": "a", "created_at": at("20:00:00", date(2026, 9, 15)),
             "valid_until": at("15:00:00")},
            {"plan_key": "late", "content_hash": "b", "created_at": at("10:00:00"), "valid_until": None},
        ])
        self.assertEqual([plan["plan_key"] for plan in plans], ["old", "late"])
        self.assertEqual(context.active_plan_index(plans, at("09:38:31")), 0)
        self.assertEqual(context.active_plan_index(plans, at("10:18:04")), 1)
        expired = [{"plan_key": "x", "created_at": at("09:00:00"), "valid_until": at("09:30:00")}]
        self.assertIsNone(context.active_plan_index(expired, at("09:38:31")))


class OrderBookTests(unittest.TestCase):
    def book(self) -> list[dict]:
        rows = [
            {"observed_at": at("09:37:58"), "price": 7.26, "bids": [{"price": 7.26, "size": 100}],
             "asks": [{"price": 7.27, "size": 900}, {"price": 0, "size": 0}], "outer_volume_lot": "1000",
             "inner_volume_lot": "900", "cumulative_amount": "1", "trade_time": "", "qi1": "-0.5", "qi5": "-0.2"},
            {"observed_at": at("09:38:28"), "price": 7.29, "bids": [{"price": 7.28, "size": 221}],
             "asks": [{"price": 7.30, "size": 1390}], "outer_volume_lot": "1300",
             "inner_volume_lot": "950", "cumulative_amount": "2", "trade_time": "", "qi1": "-0.7", "qi5": "-0.3"},
            {"observed_at": at("09:38:34"), "price": 6.00, "bids": [], "asks": [], "outer_volume_lot": "1400",
             "inner_volume_lot": "950", "cumulative_amount": "3", "trade_time": "", "qi1": None, "qi5": None},
        ]
        return context.compact_order_book(rows)

    def test_book_context_excludes_later_snapshot(self) -> None:
        book = self.book()
        self.assertEqual(book[0]["a"], [[7.27, 900.0]])
        state = context.order_book_context(book, at("09:38:31"))
        self.assertEqual(state["observed_at"], at("09:38:28"))
        self.assertEqual(state["sampled_low_so_far"], 7.26)
        self.assertEqual(state["window_net_active_lot"], 250.0)
        self.assertEqual(state["ask_depth_lot"], 1390.0)
        self.assertEqual(context.order_book_context(book, at("09:30:00")), {"status": "missing"})

    def test_minute_active_volume_is_differenced(self) -> None:
        minutes = context.order_book_minutes(self.book())
        self.assertEqual([row["minute"] for row in minutes], ["09:37", "09:38"])
        self.assertIsNone(minutes[0]["net_active_lot"])
        self.assertEqual(minutes[1]["net_active_lot"], 350.0)


class BoardAndBreadthTests(unittest.TestCase):
    def boards(self) -> dict:
        items = lambda change: [
            {"label": "化学制药", "taxonomy_key": "eastmoney_industry", "change_pct": change, "net_inflow": 1.0},
            {"label": "银行", "taxonomy_key": "eastmoney_industry", "change_pct": 0.5, "net_inflow": -2.0},
            {"label": "创新药", "taxonomy_key": "eastmoney_concept", "change_pct": -0.6, "net_inflow": 0.9},
        ]
        return context.compact_board_snapshots([
            {"observed_at": at("09:37:01"), "snapshot_minute": at("09:37:00"), "status": "completed",
             "unit": "100m_cny", "items": items(-0.4)},
            {"observed_at": at("09:39:01"), "snapshot_minute": at("09:39:00"), "status": "completed",
             "unit": "100m_cny", "items": items(1.2)},
        ])

    def test_board_labels_are_encoded_once_and_context_is_point_in_time(self) -> None:
        boards = self.boards()
        self.assertEqual(len(boards["labels"]), 3)
        state = context.board_context(boards, at("09:38:31"), ["化学制药", "不存在"])
        industry = state["taxonomies"]["eastmoney_industry"]
        self.assertEqual(industry["leaders"][0][0], "银行")
        self.assertEqual(industry["up_ratio"], 0.5)
        self.assertEqual(state["pinned"], [{"label": "化学制药", "taxonomy_key": "eastmoney_industry",
                                            "change_pct": -0.4, "net_inflow": 1.0, "rank": 2, "count": 2}])

    def test_breadth_keeps_first_computation_per_minute(self) -> None:
        minute = at("09:37:00")
        base = {"source_snapshot_minute": minute, "observed_at": at("09:37:01"), "status": "ready",
                "market_state": "flow_deterioration", "concept_count": 338, "concept_positive_ratio": "0.31",
                "concept_mean_change_pct": "-0.55", "concept_median_flow": "-0.4",
                "five_minute_positive_ratio_delta": "-0.5", "session_positive_ratio_delta": "-0.6"}
        rows = context.dedupe_breadth([{**base, "created_at": at("16:45:31")},
                                       {**base, "created_at": at("09:37:03")}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["available_at"], at("09:37:03"))
        self.assertIsNone(context.latest_available(rows, at("09:37:02")))
        self.assertEqual(context.latest_available(rows, at("09:38:31"))["concept_positive_ratio"], 0.31)


class BenchmarkTests(unittest.TestCase):
    def test_provider_tape_for_another_session_is_rejected(self) -> None:
        session = {"session_date": "2026-09-17", "rows": [{"time": "0930", "close": 3861.75}]}
        bars, status = context.benchmark_bars_from_session(session, symbol="000001.SH", day=date(2026, 9, 16),
                                                           fetched_at=at("19:00:00"))
        self.assertEqual((bars, status), ([], "provider_session_2026-09-17"))

    def test_matching_session_becomes_flat_minute_bars(self) -> None:
        session = {"session_date": "2026-09-16", "rows": [{"time": "0930", "close": 3861.75, "amount": 5.1e9},
                                                          {"time": "bad", "close": 1}]}
        bars, status = context.benchmark_bars_from_session(session, symbol="000001.SH", day=date(2026, 9, 16),
                                                           fetched_at=at("19:00:00"))
        self.assertEqual(status, "review_time_fetch")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["bar_time"], at("09:30:00"))

    def test_reference_close_prefers_same_day_pre_close(self) -> None:
        daily = [{"trading_date": date(2026, 9, 15), "close": 7.12, "pre_close": 7.3},
                 {"trading_date": date(2026, 9, 16), "close": 7.36, "pre_close": 7.12}]
        self.assertEqual(context.reference_close(daily, date(2026, 9, 16)), 7.12)
        self.assertEqual(context.reference_close(daily[:1], date(2026, 9, 16)), 7.12)
        self.assertEqual(context.previous_trading_day(daily, date(2026, 9, 16)), date(2026, 9, 15))


class FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict]:
        return self.rows


class FakeConnection:
    """Route read-only review queries by table name."""

    def __init__(self, tables: dict[str, list[dict]]) -> None:
        self.tables = tables

    def execute(self, sql: str, params: tuple) -> FakeResult:
        for table, rows in self.tables.items():
            if f"quant.{table}" in sql:
                if table == "intraday_minute_sessions" and params[0] != "600664.SH":
                    return FakeResult([])
                return FakeResult(rows)
        return FakeResult([])


class CollectReviewWiringTests(unittest.TestCase):
    def test_external_benchmark_and_early_order_gaps(self) -> None:
        day = date(2026, 9, 16)
        trade = {"trade_key": "k" * 64, "trade_date": day, "trade_time": at("09:38:31").time(), "symbol": "600664.SH",
                 "name": "哈药股份", "side": "sell", "quantity": 2000, "price": 7.28, "gross_amount": 14560,
                 "source_sha256": "s" * 64, "metadata": {"order_time": "09:38:31"}}
        board = {"observed_at": at("10:23:47"), "snapshot_minute": at("10:23:00"), "status": "completed",
                 "unit": "100m_cny", "items": [{"label": "化学制药", "taxonomy_key": "eastmoney_industry",
                                                "change_pct": 1.0, "net_inflow": 1.0}]}
        connection = FakeConnection({
            "broker_trade_records": [trade],
            "canonical_bars_daily": [{"trading_date": date(2026, 9, 15), "open": 7.26, "high": 7.4, "low": 7.1,
                                      "close": 7.12, "pre_close": 7.3, "volume": 1, "amount": 1,
                                      "selected_provider": "x", "quality_status": "fresh",
                                      "available_at": at("16:00:00", date(2026, 9, 15))}],
            "broker_portfolio_snapshots": [snapshot(at("18:26:03", date(2026, 9, 15)), 6000)],
            "intraday_board_flow_snapshots": [board],
        })
        bench = {"000001.SH": {"source": "review_time_fetch", "bars": [
            {"bar_time": at("09:30:00"), "open": 1, "high": 1, "low": 1, "close": 1, "volume": None,
             "amount": None, "source_name": "t", "available_at": at("19:00:00")}]}}
        payload = collect_review(connection, account_key="citics-primary", day=day, symbol="600664.SH",
                                 pinned_sectors=["化学制药", "不存在"], external_benchmarks=bench,
                                 external_stock_minutes=bench["000001.SH"])
        self.assertEqual(payload["coverage"]["benchmark_sources"]["000001.SH"], "review_time_fetch")
        self.assertEqual((payload["bar_source"], payload["coverage"]["stock_minute_bars"]), ("review_time_fetch", 1))
        self.assertEqual(payload["benchmarks"]["000001.SH"]["reference_close"], 7.12)
        self.assertEqual(payload["events"][0]["position"]["before_quantity"], 6000)
        gaps = " | ".join(payload["coverage"]["gaps"])
        self.assertIn("1 笔操作早于当日首个板块快照", gaps)
        self.assertIn("指定板块在当日快照中不存在：不存在", gaps)
        self.assertIn("当日没有盘口五档快照", gaps)
        self.assertNotIn("基准指数分钟线缺失", gaps)
        self.assertIn("个股分钟线为生成复盘时", gaps)


if __name__ == "__main__":
    unittest.main()
