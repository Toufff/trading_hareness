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
from pathlib import Path
from types import SimpleNamespace

from app.instrument_registry import (
    INSTRUMENT_CHUNK_SIZE,
    NAMED_INSTRUMENTS_SQL,
    ensure_instruments,
    ensure_named_instruments,
    instrument_pairs,
    named_instrument_rows,
    normalized_symbols,
    symbol_suffix_exchange,
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
        self.assertEqual(written, [("000001.SZ", "SZ"), ("600000.SH", "SH")])
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
        (``SharedLockOrderAcrossWritersTests`` below covers the writers that
        keep their own SQL, and ``test_instrument_writer_lock_order.py``
        holds every writer in the repository to the rule) and can only be
        observed against a real database.
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


class WrittenSymbolsAreTheContractTests(unittest.TestCase):
    """``normalized_symbols`` transforms, so what it returns -- not what the
    caller passed -- is the set of registry keys.  A caller that writes a
    child row carrying ``REFERENCES quant.instruments(symbol)`` in the same
    transaction must take its symbols from the return value, or the rows the
    helper stripped or dropped fail that foreign key mid-transaction.  These
    tests pin that contract rather than leaving it as docstring prose."""

    def test_returned_list_is_exactly_what_the_statement_writes(self) -> None:
        connection = _RecordingConnection()
        raw = [" 000001.SZ ", "600000.SH", "", None, "600000.SH"]
        written = ensure_instruments(connection, raw, "tushare")
        _sql, params = connection.calls[0]
        self.assertEqual([symbol for symbol, _exchange in written], params[1])
        self.assertEqual([exchange for _symbol, exchange in written], params[2])
        # And the return value is NOT the caller's input.
        self.assertNotEqual([symbol for symbol, _exchange in written], raw)

    def test_instrument_pairs_predicts_the_write_without_a_connection(self) -> None:
        connection = _RecordingConnection()
        raw = [" 600000.SH", "000001.SZ", None]
        self.assertEqual(
            instrument_pairs(raw), ensure_instruments(connection, raw, "tushare"),
        )
        self.assertEqual(instrument_pairs([], exchange_for=lambda _symbol: "SZ"), [])

    def test_universe_members_writes_back_the_registered_symbols(self) -> None:
        """``research_maintenance_service.update_universe_members`` is the
        caller the contract is about: ``quant.universe_members.symbol``
        references ``quant.instruments(symbol)``, so a padded payload symbol
        registered as ``000001.SZ`` must not be written to the child table as
        ``" 000001.SZ "``."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from app.research_maintenance_service import (
            ResearchMaintenanceDependencies,
            update_universe_members,
        )

        statements: list[tuple[str, object]] = []

        class _Transaction:
            def __enter__(self):
                return SimpleNamespace(execute=execute)

            def __exit__(self, *_exc):
                return False

        def execute(sql, params=()):
            flat = " ".join(str(sql).split())
            statements.append((flat, params))
            return MagicMock(fetchall=MagicMock(return_value=[]))

        database = MagicMock()
        database.transaction.return_value = _Transaction()
        deps = ResearchMaintenanceDependencies(
            database=database, china_today=lambda: __import__("datetime").date(2026, 9, 18),
            exchange_for=lambda symbol: symbol.rsplit(".", 1)[1],
            rebuild_analyst_research=MagicMock(), sync_universe_membership_history=MagicMock(return_value={}),
            http_exception=RuntimeError,
        )
        payload = SimpleNamespace(
            universe_key="core", symbols=[" 000001.SZ ", "600000.SH", " "], enabled=True, priority=5,
        )

        result = update_universe_members(payload, deps)

        member_symbols = [
            params[1] for sql, params in statements
            if "INSERT INTO quant.universe_members" in sql
        ]
        self.assertEqual(member_symbols, ["000001.SZ", "600000.SH"])
        self.assertEqual(result["updated"], 2)


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

    def test_every_annual_backfill_instruments_insert_orders_its_rows(self) -> None:
        """Enumerate EVERY ``INSERT INTO quant.instruments`` in the module.

        The earlier version of this test pinned one exact column list, which
        is how ``_persist_stock_basic`` -- the set-based
        ``ON CONFLICT DO UPDATE`` over the same full cross-section, i.e. the
        strongest lock of the three -- kept its seq-scan order while the two
        weaker ``DO NOTHING`` stage inserts were sorted.  Matching on the
        table name alone means a new attribute-carrying statement in this
        file fails here until it takes the shared order too.
        """
        import inspect

        from app import annual_daily_backfill

        source = inspect.getsource(annual_daily_backfill)
        statements = source.split("INSERT INTO quant.instruments")[1:]
        self.assertEqual(len(statements), 3, "a new quant.instruments writer appeared in this module")
        for statement in statements:
            head = statement.split("ON CONFLICT")[0]
            self.assertIn("ORDER BY 1", head)
        # The DO UPDATE statement is really one of them, so the loop above
        # cannot be satisfied by the two DO NOTHING inserts alone.
        conflict_actions = [statement.split("ON CONFLICT")[1][:80] for statement in statements]
        self.assertEqual(sum("DO UPDATE" in action for action in conflict_actions), 1)
        self.assertEqual(sum("DO NOTHING" in action for action in conflict_actions), 2)

    def test_annual_backfill_persist_stock_basic_binds_every_placeholder(self) -> None:
        """``_persist_stock_basic``'s lifecycle-evidence statement passed 3
        parameters for 4 placeholders, so psycopg raised before the server saw
        it and the annual ``stock_basic`` bootstrap could never complete.  The
        DB-backed test below is what found it; this one keeps the arity pinned
        in the plain suite, where no server is available."""
        from datetime import datetime, timezone

        from app.annual_daily_backfill import _persist_stock_basic

        connection = _RecordingConnection()
        _persist_stock_basic(connection, "tushare", datetime(2026, 9, 18, tzinfo=timezone.utc))
        self.assertEqual(len(connection.calls), 2)
        for sql, params in connection.calls:
            self.assertEqual(sql.count("%s"), len(params), sql[:80])

    def test_tushare_stock_basic_is_one_sorted_set_based_statement(self) -> None:
        """The ``stock_basic`` branch used to run one ``DO UPDATE`` per symbol
        in payload order.  On the Longhu post-close path that statement set
        runs BEFORE ``upsert_daily_bars`` in the same transaction, so it -- not
        the bars' already-sorted array -- is what fixes that transaction's
        lock order on ``quant.instruments``."""
        from app.tushare_normalization import STOCK_BASIC_INSTRUMENTS_SQL, persist_stock_basic_instruments

        self.assertIn("unnest(", STOCK_BASIC_INSTRUMENTS_SQL)
        self.assertIn("ORDER BY 1", STOCK_BASIC_INSTRUMENTS_SQL.split("ON CONFLICT")[0])
        self.assertIn("ON CONFLICT(symbol) DO UPDATE", STOCK_BASIC_INSTRUMENTS_SQL)
        self.assertNotIn("VALUES(%s,%s", STOCK_BASIC_INSTRUMENTS_SQL)

        connection = _RecordingConnection()
        instruments = {
            symbol: (symbol.rsplit(".", 1)[1], f"name-{symbol}", "industry", None, None, False)
            for symbol in ("600519.SH", "000001.SZ", "300750.SZ")
        }
        written = persist_stock_basic_instruments(connection, instruments, "tushare")
        self.assertEqual(written, ["000001.SZ", "300750.SZ", "600519.SH"])
        self.assertEqual(len(connection.calls), 1)
        _sql, params = connection.calls[0]
        self.assertEqual(params[0], "tushare")
        self.assertEqual(params[1], ["000001.SZ", "300750.SZ", "600519.SH"])
        self.assertEqual(params[2], ["SZ", "SZ", "SH"])
        self.assertEqual(params[3], ["name-000001.SZ", "name-300750.SZ", "name-600519.SH"])

    def test_tushare_stock_basic_chunks_keep_the_global_order(self) -> None:
        from app.tushare_normalization import persist_stock_basic_instruments

        connection = _RecordingConnection()
        symbols = [f"{index:06d}.SZ" for index in range(2, 12)]
        instruments = {symbol: ("SZ", None, None, None, None, False) for symbol in reversed(symbols)}
        persist_stock_basic_instruments(connection, instruments, "tushare", chunk_size=4)
        chunks = [params[1] for _sql, params in connection.calls]
        self.assertEqual([len(chunk) for chunk in chunks], [4, 4, 2])
        self.assertEqual([symbol for chunk in chunks for symbol in chunk], symbols)


class EnsureNamedInstrumentsTests(unittest.TestCase):
    """The second primitive: symbol + display name, ``DO UPDATE`` on the name.

    Five writers (broker order imports, broker trade imports, both
    personal-decision sites and trade discipline) each carried a byte-identical
    per-row copy of this statement, and three of the five loop over many
    symbols inside one transaction in operator/provider order.  A per-row
    ``DO UPDATE`` is the strongest lock class on this table, so those loops
    were the ones most able to deadlock against a batched writer.
    """

    def test_one_sorted_statement_per_payload(self) -> None:
        connection = _RecordingConnection()
        written = ensure_named_instruments(
            connection,
            [("600000.SH", "浦发银行"), ("000001.SZ", "平安银行")],
            "ths_desktop_export",
        )
        self.assertEqual(written, [("000001.SZ", "SZ", "平安银行"), ("600000.SH", "SH", "浦发银行")])
        self.assertEqual(len(connection.calls), 1)
        sql, params = connection.calls[0]
        self.assertIn("unnest(%s::text[],%s::text[],%s::text[])", sql)
        self.assertNotIn("VALUES(%s,%s", sql)
        self.assertIn("ORDER BY 1", sql.split("ON CONFLICT")[0])
        self.assertEqual(params, ("ths_desktop_export", ["000001.SZ", "600000.SH"], ["SZ", "SH"],
                                  ["平安银行", "浦发银行"]))

    def test_the_conflict_clause_touches_only_the_name(self) -> None:
        """The per-row statements it replaces did not update ``exchange``,
        ``source`` or ``updated_at``, so neither may this one: batching is a
        lock-order and round-trip change, not a data change."""
        self.assertIn(
            "ON CONFLICT(symbol) DO UPDATE SET "
            "name=COALESCE(NULLIF(EXCLUDED.name,''),quant.instruments.name)",
            NAMED_INSTRUMENTS_SQL,
        )
        after = NAMED_INSTRUMENTS_SQL.split("ON CONFLICT")[1]
        for column in ("exchange=", "source=", "updated_at="):
            self.assertNotIn(column, after)

    def test_a_repeated_symbol_keeps_the_last_non_blank_name(self) -> None:
        """PostgreSQL rejects a ``DO UPDATE`` that would affect the same row
        twice, so the per-row loops' last-non-blank-wins resolution has to
        happen client-side.  A symbol seen only with blank names keeps a blank,
        which the conflict clause turns back into the stored name."""
        self.assertEqual(
            named_instrument_rows([("000001.SZ", "old"), ("000001.SZ", "new")]),
            [("000001.SZ", "SZ", "new")],
        )
        self.assertEqual(
            named_instrument_rows([("000001.SZ", "kept"), ("000001.SZ", "  "), ("000001.SZ", None)]),
            [("000001.SZ", "SZ", "kept")],
        )
        self.assertEqual(
            named_instrument_rows([("000001.SZ", None), ("000001.SZ", "")]),
            [("000001.SZ", "SZ", None)],
        )

    def test_blanks_are_dropped_and_an_empty_payload_writes_nothing(self) -> None:
        connection = _RecordingConnection()
        self.assertEqual(ensure_named_instruments(connection, [], "x"), [])
        self.assertEqual(ensure_named_instruments(connection, [(None, "a"), ("  ", "b")], "x"), [])
        self.assertEqual(connection.calls, [])

    def test_the_payload_is_chunked_like_the_bare_helper(self) -> None:
        symbols = [f"{index:06d}.SZ" for index in range(1, 11)]
        connection = _RecordingConnection()
        ensure_named_instruments(
            connection, [(symbol, None) for symbol in reversed(symbols)], "x", chunk_size=4,
        )
        chunks = [params[1] for _sql, params in connection.calls]
        self.assertEqual([len(chunk) for chunk in chunks], [4, 4, 2])
        self.assertEqual([symbol for chunk in chunks for symbol in chunk], symbols)

    def test_the_suffix_exchange_resolver_is_what_the_callers_stored(self) -> None:
        """``exchange_for`` would write ``SZSE``; these five writers have
        stored ``SZ`` since before it existed, and their conflict clause never
        updates ``exchange``, so changing it would only show up on a genuinely
        new row -- silently, and only sometimes."""
        self.assertEqual(symbol_suffix_exchange("000001.SZ"), "SZ")
        self.assertEqual(symbol_suffix_exchange("600000.SH"), "SH")
        self.assertEqual(symbol_suffix_exchange("430047.BJ"), "BJ")


class _CallerFake:
    """A connection fake for the converted multi-symbol callers.

    ``rows`` maps a substring of the statement to the row its ``fetchone``
    should return; anything else returns ``None``, which is what drives these
    repositories down their "not stored yet" path.
    """

    def __init__(self, rows=None) -> None:
        self.calls: list[tuple[str, object]] = []
        self._rows = rows or {}
        self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split())
        self.calls.append((self._last, params))
        return self

    def fetchone(self):
        for needle, row in self._rows.items():
            if needle in self._last:
                return row
        return None

    def fetchall(self):
        return []

    def registrations(self):
        return [(sql, params) for sql, params in self.calls
                if sql.startswith("INSERT INTO quant.instruments")]


class NamedWriterCallersTests(unittest.TestCase):
    """The converted multi-symbol callers issue ONE registration per payload.

    The statement shape is guarded repository-wide by
    ``test_instrument_writer_lock_order.py``; what only a caller-level test can
    show is that the *loop* is gone -- that the registration was hoisted out of
    the per-record body rather than merely rewritten in place.
    """

    def test_broker_trade_batch_registers_every_record_once_ascending(self) -> None:
        from app.broker_trade_repository import persist_trade_batch

        records = [
            {"symbol": "600000.SH", "name": "浦发银行", "trade_key": "a" * 64, "side": "buy",
             "trade_date": "2026-09-18", "quantity": 100, "price": 10, "metadata": {}},
            {"symbol": "000001.SZ", "name": "平安银行", "trade_key": "b" * 64, "side": "sell",
             "trade_date": "2026-09-18", "quantity": 100, "price": 10, "metadata": {}},
        ]
        connection = _CallerFake({"SELECT count(*)": {"n": len(records)}})
        snapshot = SimpleNamespace(
            account_key="acct", observed_at=None,
            metadata={"account_identity": {"broker": "ths"}},
        )
        persist_trade_batch(
            connection,
            {"source_sha256": "s" * 64, "source": "ths_desktop_export", "records": records},
            snapshot,
        )
        self.assertEqual(len(connection.registrations()), 1)
        sql, params = connection.registrations()[0]
        self.assertIn("ORDER BY 1", sql.split("ON CONFLICT")[0])
        self.assertEqual(params[1], ["000001.SZ", "600000.SH"])
        self.assertEqual(params[3], ["平安银行", "浦发银行"])

    def test_upsert_instruments_false_still_registers_nothing(self) -> None:
        from app.broker_trade_repository import persist_trade_batch

        records = [{"symbol": "600000.SH", "name": "浦发银行", "trade_key": "a" * 64, "side": "buy",
                    "trade_date": "2026-09-18", "quantity": 100, "price": 10, "metadata": {}}]
        connection = _CallerFake({"SELECT count(*)": {"n": len(records)}})
        persist_trade_batch(
            connection,
            {"source_sha256": "s" * 64, "source": "ths_desktop_export", "records": records},
            SimpleNamespace(account_key="acct", observed_at=None,
                            metadata={"account_identity": {"broker": "ths"}}),
            upsert_instruments=False,
        )
        self.assertEqual(connection.registrations(), [])

    def test_personal_snapshot_registers_every_position_once_ascending(self) -> None:
        from app.personal_decision_repository import persist_broker_snapshot

        def _position(symbol: str, name: str):
            return SimpleNamespace(
                symbol=symbol, name=name, quantity=1, sellable_quantity=1, average_cost=1,
                market_price=1, market_value=1, unrealized_pnl=0, position_weight_pct=1, metadata={},
            )

        connection = _CallerFake({
            "RETURNING snapshot_id": {"snapshot_id": "snap-1", "observed_at": None, "verification": {}},
        })
        snapshot = SimpleNamespace(
            account_key="acct", source="manual_entry", source_snapshot_key="k",
            observed_at=None, verification={}, cash=0, total_asset=0, total_market_value=0,
            metadata={}, positions=[_position("600000.SH", "浦发银行"), _position("000001.SZ", "平安银行")],
            model_dump=lambda mode="json": {"account_key": "acct"},
        )
        persist_broker_snapshot(connection, snapshot)
        self.assertEqual(len(connection.registrations()), 1)
        sql, params = connection.registrations()[0]
        self.assertIn("ORDER BY 1", sql.split("ON CONFLICT")[0])
        self.assertEqual(params[0], "manual_entry")
        self.assertEqual(params[1], ["000001.SZ", "600000.SH"])


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
                self.assertEqual(
                    written, [(symbol, symbol.rsplit(".", 1)[1]) for symbol in sorted(all_symbols)],
                )

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


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class SortedDoUpdateWritersAgainstPostgresTests(unittest.TestCase):
    """Execute the two ``ON CONFLICT DO UPDATE`` writers this branch sorted.

    Both are the *strong* lock on ``quant.instruments`` (a DO UPDATE locks
    every existing conflicting row), and both now carry ``ORDER BY 1`` in a
    position -- between the row source and ``ON CONFLICT`` -- that nothing but
    a real server can confirm parses.  A recording fake proves the parameter
    array is ascending; only this proves the statement runs and that rows are
    written and updated as the per-row form left them.  Run against a scratch
    database (``PGDATABASE=trading_hareness_<branch>_test``), never production.
    """

    symbols = ("999811.SZ", "999812.SH", "999813.SZ")

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

    def _cleanup(self, connection) -> None:
        connection.execute(
            "DELETE FROM quant.instrument_lifecycle_evidence WHERE symbol=ANY(%s)", (list(self.symbols),),
        )
        connection.execute("DELETE FROM quant.instruments WHERE symbol=ANY(%s)", (list(self.symbols),))
        connection.commit()

    def test_tushare_stock_basic_statement_writes_and_then_updates(self) -> None:
        from app.tushare_normalization import persist_stock_basic_instruments

        from datetime import date

        with self._connect() as connection:
            try:
                self._cleanup(connection)
                first = {
                    symbol: (symbol.rsplit(".", 1)[1], f"name-{symbol}", "industry-a",
                             date(2020, 1, 2), None, False)
                    for symbol in self.symbols
                }
                written = persist_stock_basic_instruments(connection, first, "stock-basic-db-test")
                connection.commit()
                self.assertEqual(written, sorted(self.symbols))

                rows = connection.execute(
                    """SELECT symbol,exchange,name,industry,list_date,delist_date,is_st,source
                         FROM quant.instruments WHERE symbol=ANY(%s) ORDER BY symbol""",
                    (list(self.symbols),),
                ).fetchall()
                self.assertEqual([row["symbol"] for row in rows], sorted(self.symbols))
                self.assertEqual({row["source"] for row in rows}, {"stock-basic-db-test"})
                self.assertEqual({row["list_date"] for row in rows}, {date(2020, 1, 2)})
                self.assertEqual({row["is_st"] for row in rows}, {False})

                # Second payload: DO UPDATE must overwrite name/is_st and keep
                # the earlier list_date through the coalesce, exactly as the
                # per-row statement did.  A NULL name must not erase one.
                second = {
                    symbol: (symbol.rsplit(".", 1)[1], None if index else "ST renamed", "industry-b",
                             None, None, bool(index == 0))
                    for index, symbol in enumerate(sorted(self.symbols))
                }
                persist_stock_basic_instruments(connection, second, "stock-basic-db-test-2")
                connection.commit()
                updated = {
                    row["symbol"]: row
                    for row in connection.execute(
                        "SELECT symbol,name,industry,list_date,is_st,source FROM quant.instruments WHERE symbol=ANY(%s)",
                        (list(self.symbols),),
                    ).fetchall()
                }
                first_symbol = sorted(self.symbols)[0]
                self.assertEqual(updated[first_symbol]["name"], "ST renamed")
                self.assertTrue(updated[first_symbol]["is_st"])
                for symbol in sorted(self.symbols)[1:]:
                    # NULL name kept the previous value (coalesce), everything
                    # else took the new payload's.
                    self.assertEqual(updated[symbol]["name"], f"name-{symbol}")
                    self.assertFalse(updated[symbol]["is_st"])
                for row in updated.values():
                    self.assertEqual(row["industry"], "industry-b")
                    self.assertEqual(row["list_date"], date(2020, 1, 2))
                    self.assertEqual(row["source"], "stock-basic-db-test-2")
            finally:
                self._cleanup(connection)

    def test_annual_backfill_persist_stock_basic_statement_executes(self) -> None:
        """``_persist_stock_basic`` is ``INSERT ... SELECT ... ORDER BY 1 ON
        CONFLICT DO UPDATE`` straight out of the temp stage table; this is the
        only proof that ``ORDER BY`` parses in that position for the DO UPDATE
        form as well."""
        import json
        from datetime import datetime, timezone

        from app.annual_daily_backfill import _persist_stock_basic

        with self._connect() as connection:
            try:
                self._cleanup(connection)
                connection.execute(
                    "CREATE TEMP TABLE annual_daily_stage(record_index integer NOT NULL,row_data jsonb NOT NULL)",
                )
                for index, symbol in enumerate(reversed(self.symbols)):
                    connection.execute(
                        "INSERT INTO annual_daily_stage(record_index,row_data) VALUES(%s,%s::jsonb)",
                        (index, json.dumps({
                            "ts_code": symbol, "name": f"stage-{symbol}", "industry": "stage-industry",
                            "list_date": "20200102", "_list_status": "L", "trade_date": "20260918",
                        })),
                    )
                _persist_stock_basic(
                    connection, "annual-backfill-db-test", datetime(2026, 9, 18, tzinfo=timezone.utc),
                )
                connection.commit()
                rows = connection.execute(
                    "SELECT symbol,name,industry,source FROM quant.instruments WHERE symbol=ANY(%s) ORDER BY symbol",
                    (list(self.symbols),),
                ).fetchall()
                self.assertEqual([row["symbol"] for row in rows], sorted(self.symbols))
                self.assertEqual({row["source"] for row in rows}, {"annual-backfill-db-test"})
                self.assertEqual({row["name"] for row in rows}, {f"stage-{s}" for s in self.symbols})
            finally:
                connection.execute("DROP TABLE IF EXISTS annual_daily_stage")
                self._cleanup(connection)


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class NewlySortedWritersAgainstPostgresTests(SortedDoUpdateWritersAgainstPostgresTests):
    """Execute the statements this round converted, on a real server.

    Every one of them puts ``ORDER BY 1`` between a row source and
    ``ON CONFLICT`` -- a position only a server can confirm parses -- and two
    of them additionally keep a scalar placeholder *inside* the conflict
    clause while the row source is an array.  A recording fake cannot see
    either property.  Run against a scratch database, never production.
    """

    symbols = ("999821.SZ", "999822.SH", "999823.SZ")

    def test_named_helper_inserts_then_updates_only_the_name(self) -> None:
        with self._connect() as connection:
            try:
                self._cleanup(connection)
                written = ensure_named_instruments(
                    connection,
                    [(symbol, f"first-{symbol}") for symbol in reversed(self.symbols)],
                    "named-db-test",
                )
                connection.commit()
                self.assertEqual([row[0] for row in written], sorted(self.symbols))
                rows = connection.execute(
                    "SELECT symbol,exchange,name,source FROM quant.instruments "
                    "WHERE symbol=ANY(%s) ORDER BY symbol", (list(self.symbols),),
                ).fetchall()
                self.assertEqual([row["symbol"] for row in rows], sorted(self.symbols))
                self.assertEqual({row["exchange"] for row in rows}, {"SZ", "SH"})
                self.assertEqual({row["source"] for row in rows}, {"named-db-test"})

                # A second payload with a blank name for one symbol: the
                # conflict clause must keep the stored name and must not
                # rewrite exchange or source.
                ensure_named_instruments(
                    connection,
                    [(self.symbols[0], ""), (self.symbols[1], "second")],
                    "named-db-test-2",
                )
                connection.commit()
                after = {row["symbol"]: row for row in connection.execute(
                    "SELECT symbol,exchange,name,source FROM quant.instruments "
                    "WHERE symbol=ANY(%s)", (list(self.symbols),),
                ).fetchall()}
                self.assertEqual(after[self.symbols[0]]["name"], f"first-{self.symbols[0]}")
                self.assertEqual(after[self.symbols[1]]["name"], "second")
                self.assertEqual({row["source"] for row in after.values()}, {"named-db-test"})
            finally:
                self._cleanup(connection)

    def test_upsert_daily_bar_keeps_its_null_is_st_distinction(self) -> None:
        """The single-bar writer now sends arrays but still passes ``is_st``
        a second time as a scalar inside the conflict clause, because
        ``coalesce(is_st,false)`` in the row source destroys the "provider
        said nothing" case before ``EXCLUDED`` can see it."""
        from datetime import date, datetime, timezone
        from decimal import Decimal

        from app.daily_bar_repository import upsert_daily_bar
        from app.request_models import DailyBar

        symbol = self.symbols[0]

        def bar(is_st):
            return DailyBar(
                symbol=symbol, trading_date=date(2026, 9, 18), open=Decimal("10"), high=Decimal("10"),
                low=Decimal("10"), close=Decimal("10"), volume=Decimal("1000"), amount=Decimal("10000"),
                source="daily-bar-db-test", is_st=is_st, name="名称", industry="行业",
                available_at=datetime(2026, 9, 18, 8, tzinfo=timezone.utc),
            )

        with self._connect() as connection:
            try:
                self._cleanup(connection)
                connection.execute("DELETE FROM quant.market_bars_daily WHERE symbol=%s", (symbol,))
                upsert_daily_bar(connection, bar(True))
                connection.commit()
                self.assertIs(connection.execute(
                    "SELECT is_st FROM quant.instruments WHERE symbol=%s", (symbol,),
                ).fetchone()["is_st"], True)

                upsert_daily_bar(connection, bar(None))
                connection.commit()
                self.assertIs(connection.execute(
                    "SELECT is_st FROM quant.instruments WHERE symbol=%s", (symbol,),
                ).fetchone()["is_st"], True, "a NULL is_st must leave the stored flag alone")
            finally:
                connection.execute("DELETE FROM quant.raw_market_observations WHERE symbol=%s", (symbol,))
                connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=%s", (symbol,))
                connection.execute("DELETE FROM quant.market_bars_daily WHERE symbol=%s", (symbol,))
                connection.commit()
                self._cleanup(connection)

    def test_the_two_script_stage_statements_parse_and_run(self) -> None:
        """The ``scripts/`` writers are not importable here (one needs pandas
        and a CLI, the other a legacy package), so their statement is read out
        of the file and EXECUTED.  That is a real parse proof of the position
        ``ORDER BY 1`` now occupies -- not a substring assertion about it --
        and it fails if either file's statement drifts into something the
        server rejects."""
        import re

        repo_root = Path(__file__).resolve().parents[2]
        cases = (
            (repo_root / "scripts" / "import-adjusted-research-bars.py",
             "CREATE TEMP TABLE adjusted_bar_stage(symbol text)",
             "INSERT INTO adjusted_bar_stage(symbol) VALUES(%s)"),
            (repo_root / "scripts" / "legacy" / "stock_brain" / "legacy_stock_brain_repository.py",
             "CREATE TEMP TABLE stock_brain_instrument_stage(symbol text,exchange text,name text,source text)",
             "INSERT INTO stock_brain_instrument_stage VALUES(%s,'SZ','名称','stage-db-test')"),
        )
        for path, create_stage, seed in cases:
            with self.subTest(script=path.name):
                source = path.read_text(encoding="utf-8")
                match = re.search(
                    r"(INSERT INTO quant\.instruments\(.*?updated_at=now\(\)|"
                    r"INSERT INTO quant\.instruments\(.*?DO NOTHING)",
                    source, re.DOTALL,
                )
                self.assertIsNotNone(match, f"no instrument statement found in {path.name}")
                statement = match.group(1)
                self.assertIn("ORDER BY 1", statement.split("ON CONFLICT")[0])
                with self._connect() as connection:
                    try:
                        self._cleanup(connection)
                        connection.execute(create_stage)
                        for symbol in reversed(self.symbols):
                            connection.execute(seed, (symbol,))
                        connection.execute(statement)
                        connection.commit()
                        rows = connection.execute(
                            "SELECT symbol FROM quant.instruments WHERE symbol=ANY(%s) ORDER BY symbol",
                            (list(self.symbols),),
                        ).fetchall()
                        self.assertEqual([row["symbol"] for row in rows], sorted(self.symbols))
                    finally:
                        self._cleanup(connection)


if __name__ == "__main__":
    unittest.main()
