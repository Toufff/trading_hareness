"""Fill-only daily-bar gap backfill from the licensed longhu L2 history snapshot.

Pure parts run everywhere; the one database test is opt-in like the rest of the
DB-backed suite (``PGHOST``) and uses synthetic 2099 keys it cleans up itself.
"""

from __future__ import annotations

import ast
import os
import re
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from app import daily_bar_gap_backfill as backfill
from app.daily_bar_gap_backfill import (
    EvidenceStore, HistoryBar, KlineDay, beijing_limit_prices, continuity_flags, index_bars_from_kline,
    kline_agrees, limit_prices, parse_kline, parse_pankou_record,
)

APP = Path(__file__).resolve().parents[1] / "app"
FETCHED = "2026-09-20T03:40:00+00:00"


def _record(symbol="600276.SH", day="20260414", **overrides):
    real = {"open_px": 55.35, "high_px": 56.31, "low_px": 54.8, "last_px": 56.18, "total_amount": 574456,
            "total_turnover": 3184393125, "up_px": 60.72, "down_px": 49.68}
    real.update(overrides.pop("real", {}))
    record = {"symbol": symbol, "day": day, "vendor_day": int(day), "code": symbol.split(".")[0],
              "preclose_px": 55.2, "requested_at": FETCHED, "received_at": FETCHED,
              "payload_sha256": "x" * 64, "real": real}
    record.update(overrides)
    return record


def _bar(day: str, close: str, pre_close: str | None, symbol="600000.SH") -> HistoryBar:
    value = Decimal(close)
    return HistoryBar(symbol, date.fromisoformat(day), value, value, value, value,
                      None if pre_close is None else Decimal(pre_close), Decimal(100), Decimal(1), None, None,
                      "test", datetime(2026, 9, 20, tzinfo=timezone.utc), backfill.PROVIDER_KEY)


class SnapshotParsingTests(unittest.TestCase):
    def test_a_traded_session_becomes_a_raw_bar_in_the_canonical_units(self):
        bar, reason = parse_pankou_record(_record(), "600276.SH", date(2026, 4, 14))
        self.assertEqual(reason, "ok")
        # The stored tushare row of that session, field for field.
        self.assertEqual((bar.open, bar.high, bar.low, bar.close, bar.pre_close),
                         (Decimal("55.35"), Decimal("56.31"), Decimal("54.80"), Decimal("56.18"), Decimal("55.20")))
        self.assertEqual(bar.volume, Decimal(574456))                  # lots
        self.assertEqual(bar.amount, Decimal("3184393.125"))           # thousand CNY
        self.assertEqual((bar.limit_up, bar.limit_down), (Decimal("60.72"), Decimal("49.68")))
        self.assertEqual(bar.fetched_at, datetime.fromisoformat(FETCHED))
        self.assertEqual(bar.provider, backfill.PROVIDER_KEY)

    def test_the_snapshot_of_another_day_is_never_accepted(self):
        record = _record(vendor_day=20260413)
        self.assertEqual(parse_pankou_record(record, "600276.SH", date(2026, 4, 14)), (None, "vendor_day_mismatch"))
        self.assertEqual(parse_pankou_record(_record(), "600276.SH", date(2026, 4, 15))[1], "vendor_day_mismatch")

    def test_no_volume_is_not_a_session(self):
        record = _record(real={"total_amount": 0, "total_turnover": 0})
        self.assertEqual(parse_pankou_record(record, "600276.SH", date(2026, 4, 14)), (None, "no_trade"))

    def test_an_empty_payload_or_a_foreign_code_is_refused(self):
        self.assertIsNone(parse_pankou_record({"day": "20260414"}, "600276.SH", date(2026, 4, 14))[0])
        self.assertEqual(parse_pankou_record(_record(code="600277"), "600276.SH", date(2026, 4, 14))[1],
                         "vendor_code_mismatch")

    def test_impossible_ohlc_is_refused(self):
        record = _record(real={"high_px": 50.0})
        self.assertEqual(parse_pankou_record(record, "600276.SH", date(2026, 4, 14))[1], "inconsistent_ohlc")


class LimitTests(unittest.TestCase):
    def test_beijing_band_rounds_inward(self):
        # 16.45 * 1.3 = 21.385 -> 21.38 (floored), 16.45 * 0.7 = 11.515 -> 11.52 (ceiled): tushare's values;
        # the vendor served 21.37 for this one.
        self.assertEqual(beijing_limit_prices(Decimal("16.45")), (Decimal("21.38"), Decimal("11.52")))
        self.assertEqual(limit_prices("920016.BJ", Decimal("16.45"), 21.37, 11.52)[:2],
                         (Decimal("21.38"), Decimal("11.52")))

    def test_other_boards_keep_the_exchange_band_from_the_snapshot(self):
        # An ST mainboard name: the snapshot already carries its 5% band.
        self.assertEqual(limit_prices("600000.SH", Decimal("10.00"), 10.5, 9.5),
                         (Decimal("10.50"), Decimal("9.50"), "vendor_l2history_up_down_px"))

    def test_a_session_without_a_limit_is_null_not_a_sentinel(self):
        for symbol in ("301696.SZ", "920011.BJ"):
            self.assertEqual(limit_prices(symbol, Decimal("24.68"), 0, 0), (None, None, "vendor_reports_no_limit"))


class KlineCrossCheckTests(unittest.TestCase):
    def _kline(self):
        return {"symbol": "600276.SH", "requested_at": FETCHED, "received_at": FETCHED, "pages": [{
            "x": ["20260526", "20260527"], "y": [[48.88, 49.0, 50.0, 48.5], [48.88, 50.37, 51.3, 48.45]],
            "vol": [1, 2], "bal": [1000, 2000], "CQ": ["", "0,0,0,2"]}]}

    def test_a_raw_bar_before_a_dividend_agrees_with_the_forward_adjusted_candle(self):
        kline = parse_kline(self._kline())
        self.assertEqual(kline[date(2026, 5, 27)].cq, "0,0,0,2")
        bar = HistoryBar("600276.SH", date(2026, 5, 26), Decimal("49.08"), Decimal("50.20"), Decimal("48.70"),
                         Decimal("49.20"), None, None, None, None, None, "", datetime.now(timezone.utc), "p")
        self.assertTrue(kline_agrees(bar, kline))

    def test_an_unrecorded_adjustment_is_reported_not_hidden(self):
        kline = parse_kline(self._kline())
        bar = HistoryBar("600276.SH", date(2026, 5, 26), Decimal("49.14"), Decimal("50.20"), Decimal("48.70"),
                         Decimal("49.20"), None, None, None, None, None, "", datetime.now(timezone.utc), "p")
        self.assertFalse(kline_agrees(bar, kline))
        self.assertIsNone(kline_agrees(_bar("2026-05-25", "1", None), kline))


class ContinuityTests(unittest.TestCase):
    def test_ordinary_ex_date_and_boundary_checks(self):
        kline = {date(2026, 5, 27): KlineDay(date(2026, 5, 27), (0, 0, 0, 0), "0,0,0,2")}
        bars = [_bar("2026-05-26", "49.20", "49.43"), _bar("2026-05-27", "50.37", "49.00"),
                _bar("2026-05-28", "48.00", "50.37")]
        flags = continuity_flags(bars, kline, anchor_close=Decimal("49.43"), anchor_date=date(2026, 5, 25),
                                 next_pre_close=Decimal("48.00"), next_date=date(2026, 5, 29))
        self.assertEqual(flags, {date(2026, 5, 26): (), date(2026, 5, 27): ("ex_date",), date(2026, 5, 28): ()})

    def test_breaks_are_named(self):
        flags = continuity_flags([_bar("2026-05-26", "10.00", "9.90")], {}, anchor_close=Decimal("10.00"),
                                 anchor_date=date(2026, 5, 25), next_pre_close=Decimal("10.01"),
                                 next_date=date(2026, 5, 27))
        self.assertEqual(flags[date(2026, 5, 26)], ("pre_close_break_without_cq", "next_stored_pre_close_disagrees"))


class IndexTests(unittest.TestCase):
    def test_index_bars_come_from_the_kline_with_the_index_lane_name(self):
        record = {"symbol": "000001.SH", "requested_at": FETCHED, "received_at": FETCHED, "pages": [{
            "x": ["20260414", "20260415"], "y": [[4006.73, 4026.63, 4026.63, 3992.41], [4020, 4030, 4040, 4010]],
            "vol": [570259003, 1], "bal": [998435132019, 1000]}]}
        bars = index_bars_from_kline(record, "000001.SH", date(2026, 4, 15), date(2026, 4, 30))
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].provider, backfill.INDEX_PROVIDER_KEY)
        self.assertEqual((bars[0].close, bars[0].pre_close), (Decimal("4030"), Decimal("4026.63")))
        self.assertTrue(backfill.is_index("000001.SH"))
        self.assertFalse(backfill.is_index("000001.SZ"))


class EvidenceStoreTests(unittest.TestCase):
    def test_the_newest_record_wins_and_a_torn_line_is_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            store = EvidenceStore(root)
            store.append_pankou([_record(real={"last_px": 1})])
            store.append_pankou([_record()])
            with store.pankou_path("20260414").open("a", encoding="utf-8") as handle:
                handle.write('{"symbol": "600')
            self.assertEqual(store.pankou_day("20260414")["600276.SH"]["real"]["last_px"], 56.18)


class FillOnlyContractTests(unittest.TestCase):
    """The writer may insert; it may never replace, update or delete a bar."""

    def test_every_bar_and_evidence_insert_is_do_nothing(self):
        source = (APP / "daily_bar_gap_backfill.py").read_text(encoding="utf-8")
        inserts = re.findall(r"INSERT INTO quant\.(\w+).*?(?:ON CONFLICT[^\n]*)", source, re.DOTALL)
        self.assertEqual(set(inserts) & {"canonical_bars_daily", "market_bars_daily", "raw_market_observations"},
                         {"canonical_bars_daily", "market_bars_daily", "raw_market_observations"})
        for table in ("canonical_bars_daily", "market_bars_daily", "raw_market_observations"):
            statement = re.search(rf"INSERT INTO quant\.{table}\b.*?ON CONFLICT\([^)]*\) DO (\w+)", source, re.DOTALL)
            self.assertIsNotNone(statement, table)
            self.assertEqual(statement.group(1), "NOTHING", table)
        self.assertIsNone(re.search(r"(UPDATE|DELETE FROM)\s+quant\.(canonical_bars_daily|market_bars_daily|"
                                    r"raw_market_observations|instruments)", source))

    def test_the_writer_never_invents_a_factor(self):
        # The factor comes only from stored promotable evidence, re-checked per row.
        tree = ast.parse((APP / "daily_bar_gap_backfill.py").read_text(encoding="utf-8"))
        read = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "read_factors")
        self.assertIn("promotable_adjustment_factor", ast.unparse(read))
        self.assertIn("superseded_at", backfill.FACTORS_SQL)


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class PersistSessionDatabaseTests(unittest.TestCase):
    symbols = ("999981.SZ", "999982.SZ")
    trading_date = date(2099, 4, 15)

    def _cleanup(self, connection):
        symbols = list(self.symbols)
        connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=ANY(%s)", (symbols,))
        connection.execute("DELETE FROM quant.market_bars_daily WHERE symbol=ANY(%s)", (symbols,))
        connection.execute("DELETE FROM quant.raw_market_observations WHERE symbol=ANY(%s)", (symbols,))
        connection.execute("DELETE FROM quant.fetch_runs WHERE request_key LIKE 'bars-gap-backfill:unittest-%%'")
        connection.execute("DELETE FROM quant.instruments WHERE symbol=ANY(%s)", (symbols,))

    def test_existing_rows_are_kept_and_missing_rows_inserted(self):
        from app.main import DailyBar, db

        existing, missing = self.symbols
        with db.transaction() as connection:
            self._cleanup(connection)
            connection.execute("INSERT INTO quant.instruments(symbol,exchange,source) VALUES(%s,'SZ','manual')",
                               (existing,))
            connection.execute(
                """INSERT INTO quant.canonical_bars_daily(symbol,trading_date,open,high,low,close,selected_provider,
                       source_observation_ids,quality_status,available_at)
                   VALUES(%s,%s,1,1,1,1,'manual','{}','fresh',now())""", (existing, self.trading_date))
        fetched = datetime(2026, 9, 20, 4, tzinfo=timezone.utc)
        bars = [DailyBar(symbol=symbol, trading_date=self.trading_date, close=Decimal("10.5"), open=Decimal("10"),
                         high=Decimal("11"), low=Decimal("9.9"), pre_close=Decimal("10"), volume=Decimal(1000),
                         amount=Decimal("1050"), adj_factor=Decimal("1.5"), is_suspended=False,
                         limit_up=Decimal("11"), limit_down=Decimal("9"), source=backfill.PROVIDER_KEY,
                         available_at=fetched) for symbol in self.symbols]
        try:
            with db.transaction() as connection:
                counts = backfill.persist_session(connection, self.trading_date, bars, {}, run_id="unittest-1")
            self.assertEqual(counts["canonical"], 1)
            with db.transaction() as connection:
                rows = {row["symbol"]: row for row in connection.execute(
                    "SELECT * FROM quant.canonical_bars_daily WHERE symbol=ANY(%s)", (list(self.symbols),)).fetchall()}
            self.assertEqual(rows[existing]["selected_provider"], "manual")
            self.assertEqual(rows[existing]["close"], Decimal("1"))
            self.assertEqual(rows[missing]["selected_provider"], backfill.PROVIDER_KEY)
            self.assertEqual(rows[missing]["available_at"], fetched)
            self.assertEqual(rows[missing]["adj_factor"], Decimal("1.5"))
            self.assertEqual(len(rows[missing]["source_observation_ids"]), 1)
            with db.transaction() as connection:  # idempotent: a second run inserts nothing
                again = backfill.persist_session(connection, self.trading_date, bars, {}, run_id="unittest-2")
            self.assertEqual(again["canonical"], 0)
        finally:
            with db.transaction() as connection:
                self._cleanup(connection)


if __name__ == "__main__":
    unittest.main()
