from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from app.broker_order_export_parser import BrokerOrderExportError, parse_order_export
from app.broker_order_timeline import map_execution_to_bars


CN = ZoneInfo("Asia/Shanghai")


class BrokerOrderExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "orders.xls"

    def write(self, rows: str) -> Path:
        self.path.write_bytes(rows.encode("gb18030"))
        return self.path

    def sample(self) -> Path:
        return self.write(
            "委托日期\t委托时间\t证券代码\t证券名称\t买卖标志\t状态说明\t委托数量\t成交数量\t成交金额\t业务类型\t委托价格\t撤消数量\t成交价格\t委托编号\t交易市场\t已撤数量\t委托类别\t撤销标志\t资金账户\n"
            "20260915\t09:51:33\t600664\t哈药股份\t买入\t全部成交\t4000\t4000\t28920\t买入\t7.23\t0\t7.23\t1001\t上海Ａ股\t0\t限价\t委托\t0987654321\n"
            "20260915\t10:00:00\t600664\t哈药股份\t买入\t全部撤单\t2000\t0\t0\t买入\t7.10\t2000\t0\t1002\t上海Ａ股\t2000\t限价\t委托\t0987654321\n"
            "20260915\t10:01:00\t600664\t哈药股份\t买入\t撤单已成\t2000\t0\t0\t买入\t7.10\t2000\t0\t1003\t上海Ａ股\t2000\t限价\t撤单\t0987654321\n"
            "20260904\t14:06:29\t600664\t哈药股份\t卖出\t部成部撤\t3400\t2135\t16759.75\t卖出\t7.85\t1265\t7.85\t1004\t上海Ａ股\t1265\t限价\t委托\t0987654321\n"
            "20260904\t14:06:43\t600664\t哈药股份\t卖出\t撤单已成\t3400\t2135\t0\t卖出\t7.85\t1265\t0\t1005\t上海Ａ股\t1265\t限价\t撤单\t0987654321\n"
            "20260914\t18:48:01\t002212\t天融信\t买入\t全部成交\t3000\t3000\t23970\t买入\t7.99\t0\t7.99\t1006\t深圳Ａ股\t0\t限价\t委托\t0987654321\n"
        )

    def test_parses_all_order_events_but_only_strict_executions(self):
        parsed = parse_order_export(self.sample())
        self.assertEqual(len(parsed.events), 6)
        self.assertEqual(len(parsed.executions), 3)
        self.assertEqual([row["status"] for row in parsed.executions], ["全部成交", "部成部撤", "全部成交"])
        self.assertEqual(str(parsed.executions[1]["quantity"]), "2135")
        self.assertEqual(parsed.executions[0]["time_basis"], "order_time_proxy")
        self.assertEqual(parsed.min_order_date.isoformat(), "2026-09-04")
        self.assertEqual(parsed.max_order_date.isoformat(), "2026-09-15")
        self.assertTrue(parsed.account_fingerprint)
        self.assertEqual(parsed.masked_account, "09****4321")
        self.assertNotIn("0987654321", str(parsed.events))

    def test_keys_are_stable_across_repeated_exports(self):
        first = parse_order_export(self.sample())
        second = parse_order_export(self.path)
        self.assertEqual([row["event_key"] for row in first.events], [row["event_key"] for row in second.events])
        self.assertEqual([row["execution_key"] for row in first.executions], [row["execution_key"] for row in second.executions])

    def test_rejects_mixed_fund_accounts(self):
        body = self.sample().read_bytes().decode("gb18030").replace("0987654321\n", "1111111111\n", 1)
        self.write(body)
        with self.assertRaisesRegex(BrokerOrderExportError, "MULTIPLE_ACCOUNTS"):
            parse_order_export(self.path)

    def test_maps_by_first_price_cross_after_order_not_order_minute(self):
        execution = {
            "order_at": datetime(2026, 9, 15, 9, 51, 33, tzinfo=CN),
            "order_date": datetime(2026, 9, 15, tzinfo=CN).date(),
            "price": 7.23,
        }
        bars = [
            {"bar_time": datetime(2026, 9, 15, 9, 51, tzinfo=CN), "high": 7.31, "low": 7.31, "close": 7.31},
            {"bar_time": datetime(2026, 9, 15, 9, 55, tzinfo=CN), "high": 7.23, "low": 7.23, "close": 7.23},
        ]
        mapped = map_execution_to_bars(execution, bars)
        self.assertEqual(mapped["mapping_status"], "inferred_price_cross")
        self.assertEqual(mapped["mapped_bar_time"], "2026-09-15T09:55:00+08:00")
        self.assertEqual(mapped["delay_seconds"], 207)
        self.assertEqual(mapped["confidence"], "medium")

    def test_after_close_order_never_maps_to_same_day(self):
        execution = {
            "order_at": datetime(2026, 9, 14, 18, 48, 1, tzinfo=CN),
            "order_date": datetime(2026, 9, 14, tzinfo=CN).date(),
            "price": 7.99,
        }
        bars = [
            {"bar_time": datetime(2026, 9, 14, 14, 59, tzinfo=CN), "high": 7.99, "low": 7.99, "close": 7.99},
            {"bar_time": datetime(2026, 9, 15, 9, 30, tzinfo=CN), "high": 8.01, "low": 7.98, "close": 8.00},
        ]
        mapped = map_execution_to_bars(execution, bars)
        self.assertEqual(mapped["mapped_bar_time"], "2026-09-15T09:30:00+08:00")
        self.assertEqual(mapped["confidence"], "low")
        self.assertEqual(mapped["session_basis"], "after_close_next_session")


if __name__ == "__main__":
    unittest.main()
