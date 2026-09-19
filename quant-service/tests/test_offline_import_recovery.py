from __future__ import annotations

import unittest
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app.main import db, offline_import_recovery_action, offline_minute_import_stale_seconds
from app.numeric_utils import decimal_or_none
from app.offline_minute_import_service import import_csv, sha256_file


class OfflineMinuteImportRecoveryTests(unittest.TestCase):
    def test_terminal_files_are_not_reimported_and_failures_can_resume(self) -> None:
        now = datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(offline_import_recovery_action(None, now=now, stale_seconds=900), "create")
        self.assertEqual(offline_import_recovery_action({"status": "completed"}, now=now, stale_seconds=900), "unchanged")
        self.assertEqual(offline_import_recovery_action({"status": "partial"}, now=now, stale_seconds=900), "unchanged")
        self.assertEqual(offline_import_recovery_action({"status": "failed"}, now=now, stale_seconds=900), "resume_failed")

    def test_running_import_has_a_real_stale_boundary(self) -> None:
        now = datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(
            offline_import_recovery_action({"status": "running", "started_at": now - timedelta(seconds=899)}, now=now, stale_seconds=900),
            "in_progress",
        )
        self.assertEqual(
            offline_import_recovery_action({"status": "running", "started_at": now - timedelta(seconds=900)}, now=now, stale_seconds=900),
            "resume_stale_running",
        )
        self.assertEqual(
            offline_import_recovery_action({"status": "running", "started_at": None}, now=now, stale_seconds=900),
            "resume_stale_running",
        )

    def test_stale_window_is_bounded_and_sql_uses_a_hash_lock(self) -> None:
        self.assertEqual(offline_minute_import_stale_seconds({"OFFLINE_MINUTE_IMPORT_STALE_SECONDS": "1"}), 60)
        self.assertEqual(offline_minute_import_stale_seconds({"OFFLINE_MINUTE_IMPORT_STALE_SECONDS": "999999"}), 86_400)
        self.assertEqual(offline_minute_import_stale_seconds({"OFFLINE_MINUTE_IMPORT_STALE_SECONDS": "bad"}), 900)
        source = (Path(__file__).resolve().parents[1] / "app" / "offline_minute_import_service.py").read_text(encoding="utf-8")
        self.assertIn("pg_advisory_xact_lock", source)
        self.assertIn("FOR UPDATE", source)
        self.assertNotIn("from .main", source)


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class _FakeConnection:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.calls.append((flat, params))
        if "RETURNING import_id" in flat:
            return _FakeCursor({"import_id": "00000000-0000-0000-0000-000000000001"})
        return _FakeCursor(None)


class _FakeDatabase:
    def __init__(self):
        self.connection = _FakeConnection()

    def transaction(self):
        connection = self.connection

        class _Transaction:
            def __enter__(self):
                return connection

            def __exit__(self, *_args):
                return False

        return _Transaction()


class OfflineMinuteImportInstrumentBatchingTests(unittest.TestCase):
    """The import used to call the per-symbol ``ensure_instrument`` once per
    CSV row, in file order -- one statement per row and an order no other
    writer of ``quant.instruments`` shares.  Each 1,000-row slice now
    registers its symbols in one sorted, deduplicated statement."""

    def test_each_slice_registers_its_symbols_once_in_ascending_order(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "batch-minute.csv"
            lines = ["ts_code,datetime,source_available_at,open,high,low,close,vol,amount"]
            for symbol in ("600519.SH", "000001.SZ", "600519.SH", "300750.SZ"):
                lines.append(f"{symbol},2026-08-21 09:31:00,2026-08-21 09:31:05,10,11,9,10.5,100,1050")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            database = _FakeDatabase()
            result = import_csv(
                database,
                SimpleNamespace(file_name=path.name, source_name="batch-offline", max_rows=10),
                root=Path(directory), exchange_for=lambda symbol: symbol.rsplit(".", 1)[1],
                decimal_or_none=decimal_or_none, safe_error=lambda value, _limit: value,
                stale_after_seconds=900,
            )

        self.assertEqual(result["status"], "completed")
        instrument_calls = [
            (sql, params) for sql, params in database.connection.calls
            if "INSERT INTO quant.instruments" in sql
        ]
        self.assertEqual(len(instrument_calls), 1)
        sql, (source, symbols, exchanges) = instrument_calls[0]
        self.assertIn("unnest(%s::text[],%s::text[])", sql)
        self.assertNotIn("VALUES(%s,%s", sql)
        self.assertEqual(source, "offline-import")
        self.assertEqual(symbols, ["000001.SZ", "300750.SZ", "600519.SH"])
        self.assertEqual(exchanges, ["SZ", "SZ", "SH"])
        minute_writes = [params for sql, params in database.connection.calls
                         if "INSERT INTO quant.market_bars_minute" in sql]
        self.assertEqual(len(minute_writes), 4)


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class OfflineMinuteImportSqlRecoveryTests(unittest.TestCase):
    source_name = "p0-offline-import-recovery"
    symbol = "999998.SZ"

    def _clean(self, file_sha256: str) -> None:
        with db.transaction() as connection:
            connection.execute(
                "DELETE FROM quant.market_bars_minute WHERE source_name=%s AND symbol=%s",
                (self.source_name, self.symbol),
            )
            connection.execute("DELETE FROM quant.offline_imports WHERE file_sha256=%s", (file_sha256,))

    def test_failed_or_running_local_file_reuses_its_receipt_without_duplicate_import(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "p0-minute.csv"
            path.write_text(
                "ts_code,datetime,source_available_at,open,high,low,close,vol,amount\n"
                f"{self.symbol},2026-08-21 09:31:00,2026-08-21 09:31:05,10,11,9,10.5,100,1050\n",
                encoding="utf-8",
            )
            digest = sha256_file(path)
            request = SimpleNamespace(file_name=path.name, source_name=self.source_name, max_rows=10)
            arguments = {
                "root": Path(directory), "exchange_for": lambda symbol: symbol.rsplit(".", 1)[1],
                "decimal_or_none": decimal_or_none, "safe_error": lambda value, _limit: value,
                "stale_after_seconds": 900,
            }
            self._clean(digest)
            try:
                first = import_csv(db, request, **arguments)
                self.assertEqual(first["status"], "completed")
                with db.transaction() as connection:
                    connection.execute(
                        "UPDATE quant.offline_imports SET status='running',started_at=now() WHERE import_id=%s",
                        (first["import_id"],),
                    )
                active = import_csv(db, request, **arguments)
                self.assertEqual(active["status"], "running")
                with db.transaction() as connection:
                    connection.execute(
                        "UPDATE quant.offline_imports SET status='failed',error_message='interrupted' WHERE import_id=%s",
                        (first["import_id"],),
                    )
                resumed = import_csv(db, request, **arguments)
                self.assertEqual(resumed["status"], "completed")
                self.assertEqual(resumed["recovery_action"], "resume_failed")
                self.assertEqual(resumed["import_id"], first["import_id"])
                with db.transaction() as connection:
                    row = connection.execute(
                        "SELECT count(*)::int AS rows FROM quant.market_bars_minute WHERE source_name=%s AND symbol=%s",
                        (self.source_name, self.symbol),
                    ).fetchone()
                self.assertEqual(row["rows"], 1)
            finally:
                self._clean(digest)


if __name__ == "__main__":
    unittest.main()
