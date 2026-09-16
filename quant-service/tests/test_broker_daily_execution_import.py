from datetime import date
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.broker_daily_execution_import import import_daily_execution_export, finalize_daily_execution_source
from app.broker_export_parser import parse_daily_execution_export


class _Result:
    def fetchone(self):
        return {"exists": 1}

    def fetchall(self):
        return [{"symbol": "600498.SH"}]


class _Connection:
    def execute(self, statement, params=()):
        return _Result()


class DailyExecutionImportTests(unittest.TestCase):
    def test_import_requires_explicit_account_attribution_and_reads_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "inbox" / "table.xls"
            source.parent.mkdir()
            source.write_bytes((
                "成交时间\t证券代码\t证券名称\t买卖\t成交数量\t成交价格\t成交金额\t委托编号\t成交编号\t委托时间\n"
                "10:21:28\t600498\t烽火通信\t卖出\t300\t41.84\t12552\t2037197\t12345678\t10:21:25\n"
                "10:22:00\t600498\t烽火通信\t卖出\t0\t0\t0\t2037200\t0000\t10:21:59\n"
            ).encode("gb18030"))
            args = {"day": date(2026, 9, 16), "account_key": "citics-primary",
                    "broker": "中信证券", "archive_root": root / "archive"}
            with self.assertRaisesRegex(ValueError, "ACCOUNT_ATTRIBUTION_REQUIRED"):
                import_daily_execution_export(_Connection(), source,
                                              confirmed_account_attribution=False, **args)
            with patch("app.broker_daily_execution_import.persist_trade_batch",
                       return_value={"inserted": 1, "idempotent": 0, "verified_rows": 1}) as persist:
                result = import_daily_execution_export(_Connection(), source,
                                                       confirmed_account_attribution=True, **args)
            self.assertEqual((result["fill_rows"], result["zero_or_ignored_rows"]), (1, 1))
            self.assertEqual((result["inserted"], result["verified_rows"]), (1, 1))
            self.assertEqual(persist.call_args.args[1]["records"][0]["metadata"]["order_number"], "2037197")
            self.assertFalse(persist.call_args.kwargs["upsert_instruments"])
            self.assertEqual(sha256(Path(result["archive_path"]).read_bytes()).hexdigest(), result["source_sha256"])
            final = finalize_daily_execution_source(
                source, parse_daily_execution_export(source, date(2026, 9, 16)),
                day=date(2026, 9, 16), inbox_root=root / "inbox", processed_root=root / "processed")
            self.assertFalse(source.exists())
            self.assertEqual(sha256(Path(final["processed_path"]).read_bytes()).hexdigest(), result["source_sha256"])


if __name__ == "__main__":
    unittest.main()
