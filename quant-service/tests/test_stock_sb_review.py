"""Decision-time facts must not inherit future bars or later broker fills."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import MagicMock

from app.stock_sb_review import _event_context, _post_event, normalized_symbol, resolve_order_symbol
from app.stock_sb_review_page import write_review_page


UTC = timezone.utc


class StockSbReviewTests(unittest.TestCase):
    def test_symbol_normalization(self) -> None:
        self.assertEqual(normalized_symbol("600664"), "600664.SH")
        self.assertEqual(normalized_symbol("002156"), "002156.SZ")
        self.assertEqual(normalized_symbol("920123"), "920123.BJ")
        with self.assertRaises(ValueError):
            normalized_symbol("wrong")

    def test_name_resolution_scoped_to_imported_orders(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [
            {"symbol": "600664.SH", "name": "哈药股份"},
        ]
        value = resolve_order_symbol(connection, account_key="citics-primary",
                                     day=date(2026, 9, 15), stock="哈药")
        self.assertEqual(value, "600664.SH")
        self.assertEqual(connection.execute.call_args.args[1][:2],
                         ("citics-primary", date(2026, 9, 15)))

    def test_current_minute_is_excluded_from_decision(self) -> None:
        start = datetime(2026, 9, 15, 1, 30, tzinfo=UTC)
        bars = [
            {"bar_time": start, "open": 10, "high": 10, "low": 10,
             "close": 10, "volume": 100, "amount": 1000},
            {"bar_time": start + timedelta(minutes=1), "open": 10, "high": 20,
             "low": 10, "close": 20, "volume": 100, "amount": 2000},
        ]
        at = start + timedelta(minutes=1, seconds=30)
        context = _event_context(bars, at)
        self.assertEqual(context["minute_count"], 1)
        self.assertEqual(context["last_price"], 10)
        self.assertEqual(context["turnover_weighted_close_so_far"], 10)
        self.assertEqual(_post_event(bars, at, 10), {})

    def test_html_embedded_data_escapes_script_breakout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "echarts.min.js"
            asset.write_text("window.echarts={};", encoding="utf-8")
            page = write_review_page({"name": "</script><script>alert(1)</script>"}, root / "out", asset)
            body = page.read_text(encoding="utf-8")
            self.assertNotIn("</script><script>alert(1)</script>", body)
            self.assertIn("\\u003c/script\\u003e", body)
            self.assertTrue((root / "out" / "echarts.min.js").is_file())


if __name__ == "__main__":
    unittest.main()
