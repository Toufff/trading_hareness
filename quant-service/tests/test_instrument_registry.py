"""Contract tests for the batched ``quant.instruments`` registry helper.

The per-row ``INSERT ... ON CONFLICT DO NOTHING`` this helper replaces cost one
round trip per symbol (~5,547 for a full cross-section) and produced three
deadlocks in the owner PostgreSQL log on 2026-09-18 when concurrent sessions
inserted overlapping new symbols in different orders.  The two properties that
fix that -- one statement per chunk and one globally consistent (ascending)
lock order -- are asserted here, not just documented.
"""

from __future__ import annotations

import os
import unittest

from app.instrument_registry import (
    INSTRUMENT_CHUNK_SIZE,
    ensure_instruments,
    normalized_symbols,
)


class _RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class NormalizedSymbolsTests(unittest.TestCase):
    def test_duplicates_are_removed_and_order_is_ascending(self) -> None:
        self.assertEqual(
            normalized_symbols(["600000.SH", "000001.SZ", "600000.SH", "300750.SZ"]),
            ["000001.SZ", "300750.SZ", "600000.SH"],
        )

    def test_blank_and_missing_symbols_are_dropped(self) -> None:
        self.assertEqual(normalized_symbols(["", "  ", None, " 000001.SZ "]), ["000001.SZ"])

    def test_no_symbols_is_an_empty_list_not_an_error(self) -> None:
        self.assertEqual(normalized_symbols([]), [])


class EnsureInstrumentsTests(unittest.TestCase):
    def test_one_statement_for_a_whole_payload(self) -> None:
        connection = _RecordingConnection()
        written = ensure_instruments(
            connection, ["600000.SH", "000001.SZ", "600000.SH"], "tushare",
        )
        self.assertEqual(written, ["000001.SZ", "600000.SH"])
        self.assertEqual(len(connection.calls), 1)
        sql, params = connection.calls[0]
        self.assertIn("INSERT INTO quant.instruments(symbol,exchange,source)", sql)
        self.assertIn("unnest(%s::text[],%s::text[])", sql)
        self.assertIn("ON CONFLICT(symbol) DO NOTHING", sql)
        self.assertNotIn("VALUES(%s,%s", sql)
        self.assertEqual(params[0], "tushare")
        self.assertEqual(params[1], ["000001.SZ", "600000.SH"])
        self.assertEqual(params[2], ["SZ", "SH"])

    def test_symbols_are_sorted_so_concurrent_writers_share_one_lock_order(self) -> None:
        """Two callers handing over the same new symbols in opposite orders
        send the identical ascending array.

        That is all this test observes: it is a property of the parameter the
        helper builds, not a proof about PostgreSQL locking.  The ascending
        order is the *precondition* for the shared lock order; whether the
        2026-09-18 deadlock cycle is actually gone additionally depends on
        every other writer of ``quant.instruments`` taking the same order
        (see ``SharedLockOrderAcrossWritersTests`` below and the unconverted
        list in ``AGENTS.md``) and can only be observed against a real
        database.
        """
        first, second = _RecordingConnection(), _RecordingConnection()
        ensure_instruments(first, ["600519.SH", "000001.SZ", "300750.SZ"], "tushare")
        ensure_instruments(second, ["300750.SZ", "600519.SH", "000001.SZ"], "tushare")
        self.assertEqual(first.calls[0][1][1], ["000001.SZ", "300750.SZ", "600519.SH"])
        self.assertEqual(first.calls[0][1][1], second.calls[0][1][1])

    def test_empty_payload_issues_no_statement(self) -> None:
        connection = _RecordingConnection()
        self.assertEqual(ensure_instruments(connection, [], "tushare"), [])
        self.assertEqual(ensure_instruments(connection, ["", None], "tushare"), [])
        self.assertEqual(connection.calls, [])

    def test_large_payloads_are_chunked_without_losing_the_global_order(self) -> None:
        connection = _RecordingConnection()
        symbols = [f"{index:06d}.SZ" for index in range(2, 12)]
        ensure_instruments(connection, list(reversed(symbols)), "tushare", chunk_size=4)
        self.assertEqual(len(connection.calls), 3)
        chunks = [params[1] for _sql, params in connection.calls]
        self.assertEqual([len(chunk) for chunk in chunks], [4, 4, 2])
        self.assertEqual([symbol for chunk in chunks for symbol in chunk], symbols)

    def test_default_chunk_size_covers_a_full_cross_section_in_one_statement(self) -> None:
        connection = _RecordingConnection()
        self.assertGreaterEqual(INSTRUMENT_CHUNK_SIZE, 5000)
        ensure_instruments(connection, [f"{index:06d}.SZ" for index in range(1, 4001)], "tushare")
        self.assertEqual(len(connection.calls), 1)

    def test_source_and_exchange_resolver_are_caller_owned(self) -> None:
        connection = _RecordingConnection()
        ensure_instruments(
            connection, ["000001.SZ"], "eastmoney_free",
            exchange_for=lambda _symbol: "XSHG",
        )
        _sql, params = connection.calls[0]
        self.assertEqual(params[0], "eastmoney_free")
        self.assertEqual(params[2], ["XSHG"])

    def test_symbol_case_is_preserved_not_upper_cased(self) -> None:
        """Deliberate: every caller writes the very same string into a child
        table that references ``quant.instruments(symbol)`` in the same
        transaction, so upper-casing only here would desynchronize the
        registry key from the referencing row and turn a caller-side
        normalization bug into a mid-transaction foreign-key failure.
        Normalization belongs at each entry boundary -- for the HTTP surface
        that is ``request_models``, asserted in the next test."""
        self.assertEqual(normalized_symbols([" 600000.sh "]), ["600000.sh"])

    def test_api_supplied_symbols_are_upper_cased_before_they_reach_the_registry(self) -> None:
        """The minute-capture entry point is the one path that feeds
        caller-supplied symbols straight through to the registry; its request
        model is what upper-cases and validates them."""
        from app.request_models import MinuteSessionCaptureRequest

        self.assertEqual(
            MinuteSessionCaptureRequest(symbols=["600000.sh", "600000.SH"]).symbols,
            ["600000.SH"],
        )
        with self.assertRaises(Exception):
            MinuteSessionCaptureRequest(symbols=["600000"])


class SharedLockOrderAcrossWritersTests(unittest.TestCase):
    """The registry's ascending order only removes the deadlock cycle if the
    other ``quant.instruments`` writers in the same transactions take the same
    order.  ``daily_bar_batch_repository`` is the important one: its
    ``ON CONFLICT DO UPDATE`` row-locks every EXISTING conflicting row, i.e.
    the whole cross-section on any day after the first, where the registry's
    ``DO NOTHING`` locks only rows it genuinely inserts."""

    class _BatchFakeConnection(_RecordingConnection):
        """``upsert_daily_bars`` needs one observation id per input row back
        from its raw-evidence INSERT; everything else may read empty."""

        def __init__(self, row_count: int) -> None:
            super().__init__()
            self._row_count = row_count
            self._pending: list[dict[str, object]] = []

        def execute(self, sql, params=None):
            super().execute(sql, params)
            flat = " ".join(sql.split())
            self._pending = (
                [{"row_index": index, "observation_id": index + 1} for index in range(self._row_count)]
                if "quant.raw_market_observations" in flat else []
            )
            return self

        def fetchall(self):
            return self._pending

        def fetchone(self):
            return self._pending[0] if self._pending else None

    def test_batch_daily_upsert_sends_its_instrument_array_ascending(self) -> None:
        from datetime import date, datetime, timezone
        from decimal import Decimal

        from app.daily_bar_batch_repository import upsert_daily_bars
        from app.request_models import DailyBar

        connection = self._BatchFakeConnection(4)
        available_at = datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc)
        unsorted_symbols = ["600519.SH", "000001.SZ", "300750.SZ", "002594.SZ"]
        upsert_daily_bars(connection, [
            DailyBar(
                symbol=symbol, trading_date=date(2026, 9, 18), close=Decimal("10"),
                open=Decimal("10"), high=Decimal("10"), low=Decimal("10"),
                volume=Decimal("1000"), amount=Decimal("10000"),
                source="tushare", available_at=available_at,
            )
            for symbol in unsorted_symbols
        ])
        instrument_writes = [
            params for sql, params in connection.calls
            if "INSERT INTO quant.instruments" in sql and "DO UPDATE" in sql
        ]
        self.assertEqual(len(instrument_writes), 1)
        self.assertEqual(instrument_writes[0][0], sorted(unsorted_symbols))

    def test_annual_backfill_stage_inserts_order_their_rows(self) -> None:
        """Both stage inserts are ``INSERT ... SELECT DISTINCT``; without
        ``ORDER BY 1`` the rows reach ``quant.instruments`` in whatever order
        the DISTINCT node emits, which is not the shared order."""
        import inspect

        from app import annual_daily_backfill

        source = inspect.getsource(annual_daily_backfill)
        statements = [
            block for block in source.split("INSERT INTO quant.instruments(symbol,exchange,source)")[1:]
        ]
        self.assertEqual(len(statements), 2)
        for statement in statements:
            head = statement.split("ON CONFLICT")[0]
            self.assertIn("SELECT DISTINCT", head)
            self.assertIn("ORDER BY 1", head)


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class EnsureInstrumentsAgainstPostgresTests(unittest.TestCase):
    """Execute ``ENSURE_INSTRUMENTS_SQL`` against a real PostgreSQL server.

    Every other test in this file drives a recording fake, so until this one
    ran, nothing had ever proved that the ``unnest(%s::text[],%s::text[])``
    /``ON CONFLICT (symbol) DO NOTHING`` statement parses, binds two Python
    lists as text arrays, or leaves an already-registered row alone.  Run it
    against a scratch database (``PGDATABASE=trading_hareness_<branch>_test``),
    never against production.
    """

    existing_symbol = "999801.SZ"
    new_symbols = ("999803.SH", "999802.SZ")

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        from app import db_dsn

        params = db_dsn.connection_params()
        return psycopg.connect(
            host=params["host"], port=int(params["port"]), dbname=params["dbname"],
            user=params["user"], password=params["password"], row_factory=dict_row,
            connect_timeout=8,
        )

    def test_mixed_new_and_existing_payload_is_written_and_reads_back(self) -> None:
        all_symbols = [self.existing_symbol, *self.new_symbols]
        with self._connect() as connection:
            try:
                connection.execute(
                    "DELETE FROM quant.instruments WHERE symbol=ANY(%s)", (all_symbols,),
                )
                # One row already exists, written by something else.
                connection.execute(
                    "INSERT INTO quant.instruments(symbol,exchange,source) VALUES(%s,'SZSE','pre-existing')",
                    (self.existing_symbol,),
                )
                connection.commit()

                written = ensure_instruments(
                    connection,
                    # Deliberately unsorted, duplicated, blank-padded and
                    # containing the already-registered symbol.
                    [f" {self.new_symbols[0]} ", self.new_symbols[1], self.existing_symbol,
                     self.new_symbols[1], "", None],
                    "instrument-registry-db-test",
                )
                connection.commit()
                self.assertEqual(written, sorted(all_symbols))

                rows = connection.execute(
                    "SELECT symbol,exchange,source FROM quant.instruments WHERE symbol=ANY(%s) ORDER BY symbol",
                    (all_symbols,),
                ).fetchall()
                self.assertEqual([row["symbol"] for row in rows], sorted(all_symbols))
                by_symbol = {row["symbol"]: row for row in rows}
                # DO NOTHING: the pre-existing row keeps its own provenance.
                self.assertEqual(by_symbol[self.existing_symbol]["source"], "pre-existing")
                self.assertEqual(by_symbol[self.existing_symbol]["exchange"], "SZSE")
                # New rows carry this call's source and the resolved exchange.
                for symbol in self.new_symbols:
                    self.assertEqual(by_symbol[symbol]["source"], "instrument-registry-db-test")
                    self.assertEqual(by_symbol[symbol]["exchange"], symbol.rsplit(".", 1)[1])

                # Idempotent: a second identical call writes nothing new.
                ensure_instruments(connection, all_symbols, "second-call")
                connection.commit()
                sources = connection.execute(
                    "SELECT source FROM quant.instruments WHERE symbol=ANY(%s)", (all_symbols,),
                ).fetchall()
                self.assertNotIn("second-call", [row["source"] for row in sources])
            finally:
                connection.execute(
                    "DELETE FROM quant.instruments WHERE symbol=ANY(%s)", (all_symbols,),
                )
                connection.commit()


if __name__ == "__main__":
    unittest.main()
