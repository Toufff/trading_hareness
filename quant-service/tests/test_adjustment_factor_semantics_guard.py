"""Architecture guard: bar tables never receive a placeholder adjustment factor.

``quant.canonical_bars_daily.adj_factor`` and ``quant.market_bars_daily.adj_factor``
are consumed as cumulative (hfq-style) corporate-action factors -- ``close *
adj_factor`` must be comparable across dates.  A same-day identity placeholder
is not that, and because ``1.0`` is neither NULL nor ``<= 0`` it slips past
every fail-closed adjustment consumer in the service.  The rule is written in
``AGENTS.md``; this file is the test that fails when it is violated.

The source guard is deliberately an allowlist: a NEW writer that sets
``adj_factor`` on a bar table fails this test until it is reviewed and pinned
here together with the guard that keeps a placeholder out of it.

The DB-backed case proves the release/CI leak query itself, against a scratch
database, and is skipped without ``PGHOST`` exactly like the rest of the
DB-backed suite.
"""

from __future__ import annotations

import os
import re
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from app.adjustment_factor_maintenance import (
    GUARDED_BAR_TABLES,
    identity_factor_leak_sql,
)

APP = Path(__file__).resolve().parents[1] / "app"

#: Every module allowed to promote a factor onto a bar table (``SET
#: adj_factor``), with the guard that keeps a vendor placeholder out of that
#: write.  The bar upsert repositories are deliberately absent: they carry a
#: value forward from the bar payload itself and never read a factor row.
PINNED_BAR_FACTOR_WRITERS = {
    # The single factor-row -> bar-field promotion; gated on declared semantics.
    "tushare_normalization.py": "promotable_adjustment_factor(row)",
    # Range reconciliation; excludes identity rows from the DISTINCT ON candidates.
    "annual_daily_backfill.py": "<> 'same_day_identity_only'",
    # Copies only the provider that actually answered this fetch (a tushare route).
    "full_market_daily_controls_sync.py": 'results["adj_factor"].provider.key',
}

#: The producer form the rule forbids outright: no code may build a row that
#: declares a same-day identity factor.  The string may still appear as an
#: exclusion predicate or in prose.
_PLACEHOLDER_PRODUCER = re.compile(
    r"""["']factor_semantics["']\s*:\s*["']same_day_identity_only["']""")

_BAR_FACTOR_WRITE = re.compile(
    r"(canonical_bars_daily|market_bars_daily)[^;]{0,400}?SET\s+adj_factor|"
    r"SET\s+adj_factor[^;]{0,400}?(canonical_bars_daily|market_bars_daily)",
    re.IGNORECASE | re.DOTALL,
)


class BarFactorWriterAllowlistTests(unittest.TestCase):
    def test_only_pinned_modules_write_a_factor_onto_a_bar_table(self):
        writers = set()
        for path in sorted(APP.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "adj_factor" not in text:
                continue
            if _BAR_FACTOR_WRITE.search(text):
                writers.add(path.name)
        self.assertEqual(
            writers, set(PINNED_BAR_FACTOR_WRITERS),
            "a module started writing adj_factor onto a bar table; pin it here together "
            "with the guard that keeps a vendor placeholder out of that write")

    def test_each_pinned_writer_still_carries_its_placeholder_guard(self):
        for name, guard in PINNED_BAR_FACTOR_WRITERS.items():
            with self.subTest(module=name):
                self.assertIn(guard, (APP / name).read_text(encoding="utf-8"))

    def test_no_code_path_produces_an_identity_adjustment_factor_row(self):
        for path in sorted(APP.rglob("*.py")):
            with self.subTest(module=path.name):
                self.assertIsNone(
                    _PLACEHOLDER_PRODUCER.search(path.read_text(encoding="utf-8")),
                    f"{path.name} builds a same-day identity factor row; the vendor supplies no "
                    "corporate-action history and an identity placeholder is promoted onto the "
                    "bar tables exactly like a real cumulative factor")

    def test_the_bar_upsert_repositories_never_read_a_factor_row(self):
        # Carry-forward only: an absent factor in the bar payload keeps whatever
        # the row already had.  Neither repository ever invents a factor or
        # reads one out of quant.daily_adjustment_factors.
        carry_forward = {
            "daily_bar_repository.py": "adj_factor=coalesce(EXCLUDED.adj_factor",
            "daily_bar_batch_repository.py": "bar.adj_factor if bar.adj_factor is not None else",
        }
        for name, expected in carry_forward.items():
            with self.subTest(module=name):
                text = (APP / name).read_text(encoding="utf-8")
                self.assertNotIn("daily_adjustment_factors", text)
                self.assertIn(expected, text)

    def test_the_vendor_control_builder_returns_no_adjustment_factor_key(self):
        from app.longhu_market_sync import build_control_rows

        controls = build_control_rows([
            {"ts_code": "600664.SH", "trade_date": "20260918", "pre_close": 10, "name": "t"}])
        self.assertEqual(set(controls), {"stk_limit"})

    def test_the_leak_query_is_pinned_to_the_two_bar_tables(self):
        for table in GUARDED_BAR_TABLES:
            self.assertIn(f"quant.{table} bar", identity_factor_leak_sql(table))
        with self.assertRaises(ValueError):
            identity_factor_leak_sql("instruments")


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class IdentityFactorLeakQueryTests(unittest.TestCase):
    """Prove the release guard query on real PostgreSQL, on a fixture-only key."""

    symbol = "999981.SZ"
    trading_date = date(2099, 4, 1)

    def _cleanup(self, connection) -> None:
        connection.execute(
            "DELETE FROM quant.daily_adjustment_factors WHERE symbol=%s", (self.symbol,))
        for table in GUARDED_BAR_TABLES:
            connection.execute(
                f"DELETE FROM quant.{table} WHERE symbol=%s", (self.symbol,))
        connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))

    def _leaks(self, connection, table) -> int:
        return int(connection.execute(identity_factor_leak_sql(table)).fetchone()["identity_leaks"])

    def test_query_finds_a_placeholder_only_bar_and_clears_once_evidence_is_real(self):
        from psycopg.types.json import Json

        from app.main import db

        observed = datetime(2099, 4, 1, 7, tzinfo=timezone.utc)
        with db.transaction() as connection:
            self._cleanup(connection)
            baseline = {table: self._leaks(connection, table) for table in GUARDED_BAR_TABLES}

            connection.execute(
                "INSERT INTO quant.instruments(symbol,exchange,source) VALUES(%s,'SZ','test') "
                "ON CONFLICT(symbol) DO NOTHING", (self.symbol,))
            connection.execute(
                """INSERT INTO quant.daily_adjustment_factors(
                       symbol,trading_date,adj_factor,provider,available_at,raw)
                   VALUES(%s,%s,1,'longhuvip_composite',%s,%s)""",
                (self.symbol, self.trading_date, observed,
                 Json({"factor_semantics": "same_day_identity_only"})))
            connection.execute(
                """INSERT INTO quant.canonical_bars_daily(
                       symbol,trading_date,close,adj_factor,selected_provider,quality_status,available_at)
                   VALUES(%s,%s,10,1,'longhuvip_composite','fresh',%s)""",
                (self.symbol, self.trading_date, observed))

            self.assertEqual(
                self._leaks(connection, "canonical_bars_daily"),
                baseline["canonical_bars_daily"] + 1,
                "the guard query must see a bar whose only factor evidence is a placeholder")

            # NULL is what the repair writes: "no adjustment information".
            connection.execute(
                "UPDATE quant.canonical_bars_daily SET adj_factor=NULL "
                "WHERE symbol=%s AND trading_date=%s", (self.symbol, self.trading_date))
            self.assertEqual(self._leaks(connection, "canonical_bars_daily"),
                             baseline["canonical_bars_daily"])

            # A real tushare factor makes the same bar legitimate again.
            connection.execute(
                """INSERT INTO quant.daily_adjustment_factors(
                       symbol,trading_date,adj_factor,provider,available_at,raw)
                   VALUES(%s,%s,12.5,'tushare_super_get',%s,%s)""",
                (self.symbol, self.trading_date, observed, Json({})))
            connection.execute(
                "UPDATE quant.canonical_bars_daily SET adj_factor=12.5 "
                "WHERE symbol=%s AND trading_date=%s", (self.symbol, self.trading_date))
            self.assertEqual(self._leaks(connection, "canonical_bars_daily"),
                             baseline["canonical_bars_daily"])

            self._cleanup(connection)


if __name__ == "__main__":
    unittest.main()
