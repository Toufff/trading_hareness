import sqlite3
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest

from app.legacy_stock_brain_archive import create_consistent_snapshot, sqlite_tables
from app.legacy_stock_brain_contracts import (
    normalize_symbol,
    normalized_payload,
    payload_sha256,
    table_classification,
)


class LegacyStockBrainContractTests(unittest.TestCase):
    def test_symbol_normalization_preserves_global_context(self):
        self.assertEqual(normalize_symbol("sh600664"), "600664.SH")
        self.assertEqual(normalize_symbol("002212"), "002212.SZ")
        self.assertEqual(normalize_symbol("830001"), "830001.BJ")
        self.assertEqual(normalize_symbol("^SOX"), "^SOX")

    def test_failed_orchestration_is_excluded_but_evidence_is_archived(self):
        self.assertIsNone(table_classification("decision_session_runs"))
        self.assertEqual(table_classification("documents"), "durable_fact")
        self.assertEqual(table_classification("alpha_scores"), "research_evidence")

    def test_payload_hash_is_stable_and_binary_safe(self):
        first = normalized_payload({"id": 1, "blob": b"abc"})
        second = normalized_payload({"blob": b"abc", "id": 1})
        self.assertEqual(payload_sha256(first), payload_sha256(second))

    def test_postgres_incompatible_text_is_preserved_reversibly(self):
        payload = normalized_payload({"text": "before\x00after"})
        self.assertEqual(payload["text"]["encoding"], "utf-8-base64")
        self.assertEqual(payload["text"]["reason"], "postgresql-text-incompatible")

    def test_snapshot_is_consistent_and_fts_tables_are_not_migrated(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            connection = sqlite3.connect(source)
            connection.execute("CREATE TABLE documents(id integer primary key,title text)")
            connection.execute("INSERT INTO documents(title) VALUES('证据')")
            connection.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(title)")
            connection.commit()
            connection.close()
            snapshot, digest = create_consistent_snapshot(source, Path(directory) / "archive")
            self.assertEqual(len(digest), 64)
            copied = sqlite3.connect(snapshot)
            try:
                self.assertEqual(sqlite_tables(copied), ["documents"])
                self.assertEqual(copied.execute("SELECT title FROM documents").fetchone()[0], "证据")
            finally:
                copied.close()


if __name__ == "__main__":
    unittest.main()
