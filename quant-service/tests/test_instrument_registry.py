"""Contract tests for the batched ``quant.instruments`` registry helper.

The per-row ``INSERT ... ON CONFLICT DO NOTHING`` this helper replaces cost one
round trip per symbol (~5,547 for a full cross-section) and produced three
deadlocks in the owner PostgreSQL log on 2026-09-18 when concurrent sessions
inserted overlapping new symbols in different orders.  The two properties that
fix that -- one statement per chunk and one globally consistent (ascending)
lock order -- are asserted here, not just documented.
"""

from __future__ import annotations

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
        must send the identical ascending array; that consistent lock order is
        what removes the 2026-09-18 deadlock cycle."""
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


if __name__ == "__main__":
    unittest.main()
