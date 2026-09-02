import unittest
from unittest.mock import patch

import database_bootstrap
from database_bootstrap import (
    BASELINE_PREREQUISITES_SQL,
    BASELINE_REVISION,
    REQUIRED_BASELINE_TABLES,
    bootstrap_action,
    initialize_database,
)


class _Cursor:
    def __init__(self, connection):
        self._connection = connection
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split())
        self._connection.statements.append(self._last)

    def fetchone(self):
        return self._connection.answer(self._last)


class _Transaction:
    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        self._connection.statements.append("BEGIN")
        return self

    def __exit__(self, *exc):
        self._connection.statements.append("ROLLBACK" if exc[0] else "COMMIT")
        return False


class _Connection:
    """Scripted psycopg stand-in: answers schema-state probes from a small state model."""

    def __init__(self, *, versioned=False, quant_tables=0, required_tables=0, ledger_present=True):
        self.versioned = versioned
        self.quant_tables = quant_tables
        self.required_tables = required_tables
        self.ledger_present = ledger_present
        self.statements = []
        self.executed_ddl = []
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def transaction(self):
        return _Transaction(self)

    def execute(self, sql, params=None):
        # Connection-level execute is only used for the frozen baseline DDL.
        self.executed_ddl.append(sql)
        self.statements.append("DDL")
        if sql is not BASELINE_PREREQUISITES_SQL:
            self.quant_tables = 120
            self.required_tables = len(REQUIRED_BASELINE_TABLES)

    def close(self):
        self.closed = True

    def answer(self, sql):
        if "pg_try_advisory_lock" in sql:
            return (True,)
        if "pg_advisory_unlock" in sql:
            return (True,)
        if "to_regclass('quant.alembic_version')" in sql:
            return (self.versioned,)
        if "FROM pg_tables WHERE schemaname='quant'" in sql:
            return (self.quant_tables,)
        if "unnest(%s::text[])" in sql:
            return (self.required_tables,)
        if "to_regclass('public.ingestion_jobs')" in sql:
            return (self.ledger_present,)
        if "SELECT version_num FROM quant.alembic_version" in sql:
            return ("20260902_0085",)
        raise AssertionError(f"unexpected query: {sql}")


class DatabaseBootstrapTests(unittest.TestCase):
    def test_versioned_schema_only_runs_upgrades(self):
        self.assertEqual(
            bootstrap_action(
                version_table_present=True,
                quant_table_count=200,
                required_table_count=len(REQUIRED_BASELINE_TABLES),
            ),
            "upgrade",
        )

    def test_empty_schema_creates_frozen_baseline(self):
        self.assertEqual(
            bootstrap_action(
                version_table_present=False,
                quant_table_count=0,
                required_table_count=0,
            ),
            "create_baseline",
        )

    def test_complete_unversioned_baseline_is_resumable(self):
        self.assertEqual(
            bootstrap_action(
                version_table_present=False,
                quant_table_count=len(REQUIRED_BASELINE_TABLES),
                required_table_count=len(REQUIRED_BASELINE_TABLES),
            ),
            "stamp_existing",
        )

    def test_partial_unversioned_schema_is_never_stamped(self):
        with self.assertRaisesRegex(RuntimeError, "partial"):
            bootstrap_action(
                version_table_present=False,
                quant_table_count=3,
                required_table_count=2,
            )

    def test_fresh_baseline_prerequisites_define_sector_parents(self):
        self.assertLess(
            BASELINE_PREREQUISITES_SQL.index("quant.sector_taxonomies"),
            BASELINE_PREREQUISITES_SQL.index("quant.sectors"),
        )

    def test_prerequisites_create_schema_and_pgcrypto_before_tables(self):
        self.assertLess(BASELINE_PREREQUISITES_SQL.index("CREATE EXTENSION IF NOT EXISTS pgcrypto"),
                        BASELINE_PREREQUISITES_SQL.index("CREATE TABLE"))
        self.assertLess(BASELINE_PREREQUISITES_SQL.index("CREATE SCHEMA IF NOT EXISTS quant"),
                        BASELINE_PREREQUISITES_SQL.index("CREATE TABLE"))


class InitializeDatabaseTests(unittest.TestCase):
    def _run(self, connection):
        alembic_calls = []
        with patch.object(database_bootstrap, "database_connection", return_value=connection), \
                patch.object(database_bootstrap, "_run_alembic", lambda *args: alembic_calls.append(args)):
            result = initialize_database()
        return result, alembic_calls

    def test_empty_database_creates_baseline_stamps_then_upgrades(self):
        connection = _Connection()
        result, alembic_calls = self._run(connection)

        self.assertEqual(result, {"status": "ready", "action": "create_baseline", "revision": "20260902_0085"})
        self.assertEqual(alembic_calls, [("stamp", BASELINE_REVISION), ("upgrade", "head")])
        # Prerequisites then the frozen DDL, inside one transaction.
        self.assertIs(connection.executed_ddl[0], BASELINE_PREREQUISITES_SQL)
        self.assertTrue(connection.executed_ddl[1].startswith(database_bootstrap.SCHEMA_SQL))
        self.assertTrue(connection.executed_ddl[1].endswith(database_bootstrap.PLATFORM_SCHEMA_SQL))
        begin = connection.statements.index("BEGIN")
        commit = connection.statements.index("COMMIT")
        self.assertEqual(connection.statements[begin:commit + 1], ["BEGIN", "DDL", "DDL", "COMMIT"])
        # The advisory lock brackets everything and the connection is closed.
        self.assertTrue(connection.statements[0].startswith("SELECT pg_try_advisory_lock"))
        self.assertTrue(connection.statements[-1].startswith("SELECT pg_advisory_unlock"))
        self.assertTrue(connection.closed)

    def test_versioned_database_only_upgrades(self):
        connection = _Connection(versioned=True, quant_tables=150, required_tables=len(REQUIRED_BASELINE_TABLES))
        result, alembic_calls = self._run(connection)

        self.assertEqual(result["action"], "upgrade")
        self.assertEqual(alembic_calls, [("upgrade", "head")])
        self.assertEqual(connection.executed_ddl, [])
        self.assertNotIn("BEGIN", connection.statements)

    def test_complete_unversioned_baseline_is_stamped_not_recreated(self):
        connection = _Connection(quant_tables=90, required_tables=len(REQUIRED_BASELINE_TABLES))
        result, alembic_calls = self._run(connection)

        self.assertEqual(result["action"], "stamp_existing")
        self.assertEqual(alembic_calls, [("stamp", BASELINE_REVISION), ("upgrade", "head")])
        self.assertEqual(connection.executed_ddl, [])

    def test_missing_ingestion_ledger_blocks_fresh_bootstrap(self):
        connection = _Connection(ledger_present=False)
        with self.assertRaisesRegex(RuntimeError, "initialize-ledger"):
            self._run(connection)
        self.assertEqual(connection.executed_ddl, [])
        self.assertTrue(connection.statements[-1].startswith("SELECT pg_advisory_unlock"))
        self.assertTrue(connection.closed)

    def test_partial_schema_releases_lock_and_raises(self):
        connection = _Connection(quant_tables=3, required_tables=2)
        with self.assertRaisesRegex(RuntimeError, "partial"):
            self._run(connection)
        self.assertTrue(connection.statements[-1].startswith("SELECT pg_advisory_unlock"))
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
