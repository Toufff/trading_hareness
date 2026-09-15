import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from app.broker_desktop_evidence import account_fingerprint, load_manual_envelope, prepare_export_envelope
from app.broker_export_parser import BrokerExportError, parse_account_export, parse_holdings_export, parse_trade_export
from app.broker_trade_repository import load_trade_batch, persist_trade_batch


class BrokerExportParserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.run = {"run_id": "run-export-1", "input_summary": {"account_key": "selected-account"}}

    def write(self, name, text):
        path = (self.root / name).resolve()
        path.write_bytes(text.encode("gb18030"))
        return path

    def exports(self):
        holdings = self.write(
            "holdings.xls",
            "证券代码\t证券名称\t实际数量\t可用股份\t成本价\t市价\t证券市值\t浮动盈亏\n"
            "600664\t哈药股份\t1000\t1000\t7.20\t7.50\t7500\t300\n"
            "000977\t浪潮信息\t0\t0\t40\t41\t0\t0\n",
        )
        account = self.write("account.xls", "总资产\t10000\t可用资金\t2500\t股票市值\t7500\n")
        trades = self.write(
            "statement.xls",
            "交易日期\t成交时间\t证券代码\t证券名称\t备注\t成交数量\t成交价格\t成交金额\t发生金额\t手续费\t印花税\t过户费\t货币单位\n"
            "20260915\t093012\t600664\t哈药股份\t证券买入\t1000\t7.20\t7200\t-7201.50\t1.50\t0\t0\t人民币\n"
            "20260915\t100000\t600664\t哈药股份\t撤单\t100\t7.10\t710\t0\t0\t0\t0\t人民币\n",
        )
        return holdings, account, trades

    def manifest(self):
        value = {
            "schema_version": "broker-export-session-v1", "run_id": self.run["run_id"],
            "account_key": "selected-account", "observed_at": self.now.isoformat(),
            "trade_date": self.now.date().isoformat(),
            "account_identity": {"broker": "中信证券", "account_fingerprint": account_fingerprint("中信证券", "12345678"),
                                 "masked_account": "12****78"},
            "account_binding": {"method": "existing_account_evidence_match", "existing_snapshot_id": "old"},
        }
        path = (self.root / "session.json").resolve()
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_parse_text_xls_holdings_account_and_trades(self):
        holdings, account, trades = self.exports()
        parsed = parse_holdings_export(holdings)
        self.assertEqual([row["symbol"] for row in parsed.rows], ["600664.SH"])
        self.assertEqual(parsed.rows[0]["sellable_quantity"], 1000)
        self.assertEqual(parsed.ignored_rows[0]["reason"], "zero_actual_quantity")
        totals = parse_account_export(account)
        self.assertEqual(str(totals["total_market_value"]), "7500")
        fills = parse_trade_export(trades)
        self.assertEqual((fills.rows[0]["side"], fills.rows[0]["symbol"]), ("buy", "600664.SH"))
        self.assertEqual(fills.ignored_rows[0]["reason"], "cancelled_or_invalid")

    def test_prepare_and_reparse_envelope(self):
        holdings, account, trades = self.exports()
        result = prepare_export_envelope(self.run, self.manifest(), holdings, account, self.root / "out",
                                         trade_path=trades, now=self.now)
        self.assertEqual((result["position_count"], result["trade_count"]), (1, 1))
        snapshot = load_manual_envelope(result["envelope"], self.run, now=self.now)
        self.assertEqual(snapshot.source, "ths_desktop_export")
        self.assertEqual(snapshot.positions[0].name, "哈药股份")
        batch = load_trade_batch(Path(result["trade_batch"]).resolve(), self.run, snapshot)
        self.assertEqual(len(batch["records"]), 1)
        value = json.loads(Path(result["envelope"]).read_text(encoding="utf-8"))
        value["positions"][0]["quantity"] = "9999"
        Path(result["envelope"]).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "HOLDINGS_MISMATCH"):
            load_manual_envelope(result["envelope"], self.run, now=self.now)

    def test_trade_batch_cannot_replace_raw_export(self):
        holdings, account, trades = self.exports()
        result = prepare_export_envelope(self.run, self.manifest(), holdings, account, self.root / "out",
                                         trade_path=trades, now=self.now)
        snapshot = load_manual_envelope(result["envelope"], self.run, now=self.now)
        value = json.loads(Path(result["trade_batch"]).read_text(encoding="utf-8"))
        value["records"][0]["quantity"] = "9999"
        Path(result["trade_batch"]).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ROWS_MISMATCH"):
            load_trade_batch(Path(result["trade_batch"]).resolve(), self.run, snapshot)

    def test_trade_persistence_is_idempotent_and_reads_back(self):
        _, _, trades = self.exports()
        records = parse_trade_export(trades).rows
        batch = {"source": "ths_desktop_export", "source_sha256": parse_trade_export(trades).sha256,
                 "records": records}
        snapshot = SimpleNamespace(
            account_key="selected-account", observed_at=self.now,
            metadata={"account_identity": {"broker": "中信证券"}},
        )

        class Result:
            def __init__(self, row=None): self.row = row
            def fetchone(self): return self.row

        class Database:
            def __init__(self): self.rows = {}
            def execute(self, statement, params=()):
                sql = " ".join(str(statement).split())
                if sql.startswith("SELECT source_sha256"):
                    row = self.rows.get(params[1])
                    return Result(row)
                if sql.startswith("INSERT INTO quant.broker_trade_records"):
                    self.rows[params[1]] = {"source_sha256": params[18], "metadata": params[20].obj}
                    return Result()
                if sql.startswith("SELECT count(*)"):
                    return Result({"n": sum(key in self.rows for key in params[1])})
                return Result()

        database = Database()
        first = persist_trade_batch(database, batch, snapshot)
        second = persist_trade_batch(database, batch, snapshot)
        self.assertEqual((first["inserted"], first["verified_rows"]), (1, 1))
        self.assertEqual((second["inserted"], second["idempotent"]), (0, 1))

    def test_reject_binary_xls_and_missing_totals(self):
        binary = (self.root / "binary.xls").resolve()
        binary.write_bytes(b"\xd0\xcf\x11\xe0test")
        with self.assertRaisesRegex(BrokerExportError, "BINARY"):
            parse_holdings_export(binary)
        account = self.write("bad-account.xls", "总资产\t100\n")
        with self.assertRaisesRegex(BrokerExportError, "TOTALS_MISSING"):
            parse_account_export(account)


if __name__ == "__main__":
    unittest.main()
