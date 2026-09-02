"""Structural guards for the Alembic chain and the frozen legacy DDL.

No database is needed: migrations are imported from disk and ``alembic.op``
is stubbed so every emitted statement can be inspected.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.util
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import alembic.op

from app.database import PLATFORM_SCHEMA_SQL, SCHEMA_SQL


VERSIONS = Path(__file__).parents[1] / "migrations" / "versions"

# The legacy DDL is frozen: new schema goes through migrations only.  Update
# these digests deliberately, together with a migration that mirrors the change.
FROZEN_SCHEMA_SQL_SHA256 = "27529de25825b3e8b6073db6eb8d5e0f847da30362d7f7bb361d448a4650d534"
FROZEN_PLATFORM_SCHEMA_SQL_SHA256 = "e58cbb0490f9744252ce312b773fa2fa7527440d6a5a9851b3d952e69be92e34"

# Tables that are (historically) created by both the frozen DDL and a
# migration.  The set must not grow: a new table belongs in a migration only.
TABLES_IN_BOTH_DDL_AND_MIGRATIONS = frozenset({
    "alert_delivery_health_events", "analyst_sync_attempts", "daily_market_aggregates",
    "instrument_lifecycle_evidence", "intraday_board_rotation_deliveries", "intraday_board_rotation_events",
    "intraday_rule_input_snapshots", "intraday_scan_rejections", "market_flow_feature_snapshots",
    "post_close_strategy_screen_observations", "remote_analyst_message_versions", "remote_analyst_messages",
    "sector_flow_daily_features", "sector_flow_daily_outcomes", "strategy_day_summaries",
    "ten_day_leader_rotation_candidates", "ten_day_leader_rotation_runs", "universe_membership_history",
})
LEGACY_DDL_ADD_COLUMN_COUNT = 10


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migrations() -> list:
    return [_load(path) for path in sorted(VERSIONS.glob("*.py"))]


def _statements(function, *, autocommit: bool = False) -> tuple[list[str], list[str]]:
    """Run a migration function with a stubbed ``op``; return (all, autocommit-only) statements."""
    statements: list[str] = []
    autocommit_statements: list[str] = []
    state = {"autocommit": False}

    @contextmanager
    def autocommit_block():
        state["autocommit"] = True
        try:
            yield
        finally:
            state["autocommit"] = False

    def execute(sql, *args, **kwargs):
        text = " ".join(sql.split())
        statements.append(text)
        if state["autocommit"]:
            autocommit_statements.append(text)

    with patch.object(alembic.op, "execute", execute), \
            patch.object(alembic.op, "get_context", lambda: SimpleNamespace(autocommit_block=autocommit_block)):
        function()
    return statements, autocommit_statements


def _table_block(sql: str, table: str) -> str:
    match = re.search(rf"CREATE TABLE IF NOT EXISTS quant\.{table} \((.*?)\n\s*\)", sql, re.S)
    assert match, f"no CREATE TABLE for quant.{table}"
    return match.group(1)


class MigrationChainTests(unittest.TestCase):
    def setUp(self):
        self.modules = _migrations()

    def test_revisions_match_filenames_and_form_one_linear_chain(self):
        by_revision = {}
        for module in self.modules:
            stem = Path(module.__file__).stem
            self.assertTrue(stem.startswith(module.revision), f"{stem} does not start with {module.revision}")
            self.assertRegex(module.revision, r"^\d{8}_\d{4}$")
            self.assertNotIn(module.revision, by_revision, f"duplicate revision {module.revision}")
            by_revision[module.revision] = module
        roots = [m for m in self.modules if m.down_revision is None]
        self.assertEqual([m.revision for m in roots], ["20260811_0001"])
        parents = [m.down_revision for m in self.modules if m.down_revision is not None]
        self.assertEqual(len(parents), len(set(parents)), "two revisions share a parent: multiple heads")
        heads = [m.revision for m in self.modules if m.revision not in set(parents)]
        self.assertEqual(len(heads), 1, f"multiple heads: {heads}")
        for parent in parents:
            self.assertIn(parent, by_revision, f"dangling down_revision {parent}")

    def test_baseline_creates_schema_and_pgcrypto_idempotently(self):
        baseline = next(m for m in self.modules if m.revision == "20260811_0001")
        statements, _ = _statements(baseline.upgrade)
        self.assertEqual(statements, [
            "CREATE EXTENSION IF NOT EXISTS pgcrypto",
            "CREATE SCHEMA IF NOT EXISTS quant",
        ])
        self.assertEqual(_statements(baseline.downgrade)[0], [])

    def test_add_constraint_statements_are_guarded_by_pg_constraint(self):
        for module in self.modules:
            statements, _ = _statements(module.upgrade)
            for sql in statements:
                for match in re.finditer(r"ALTER TABLE quant\.(\w+) ADD CONSTRAINT (\w+)", sql):
                    table, name = match.groups()
                    guard = rf"IF NOT EXISTS \(SELECT 1 FROM pg_constraint WHERE conname='{name}' AND conrelid='quant\.{table}'::regclass\)"
                    self.assertRegex(sql, guard, f"{module.revision}: unguarded ADD CONSTRAINT {name}")

    def test_0017_adds_validated_constraints_in_one_step(self):
        module = next(m for m in self.modules if m.revision == "20260813_0017")
        statements, _ = _statements(module.upgrade)
        joined = "\n".join(statements)
        self.assertNotIn("NOT VALID", joined)
        self.assertNotIn("VALIDATE CONSTRAINT", joined)
        self.assertEqual(len(re.findall(r"ADD CONSTRAINT", joined)), 2)


class HotTableIndexMigrationTests(unittest.TestCase):
    def setUp(self):
        self.module = _load(VERSIONS / "20260902_0084_hot_table_lookup_indexes.py")
        self.signal_episodes_ddl = (VERSIONS / "20260814_0021_signal_episodes.py").read_text(encoding="utf-8")

    def test_every_index_is_created_concurrently_inside_autocommit(self):
        statements, autocommit = _statements(self.module.upgrade)
        self.assertEqual(statements, autocommit)
        self.assertEqual(len(statements), len(self.module.INDEXES))
        for sql in statements:
            self.assertRegex(sql, r"^CREATE INDEX CONCURRENTLY IF NOT EXISTS \w+ ON quant\.\w+ ")
        expected = {
            "tushare_raw_api_ts_code_idx", "intraday_signal_events_symbol_observed_idx",
            "intraday_signal_events_observed_idx", "intraday_signal_events_scan_idx",
            "data_quality_issues_symbol_date_idx", "intraday_signal_episodes_symbol_session_idx",
            "market_bars_minute_bar_time_brin_idx",
        }
        self.assertEqual({name for name, _, _ in self.module.INDEXES}, expected)

    def test_indexed_columns_exist_on_their_tables(self):
        for name, table, definition in self.module.INDEXES:
            table_name = table.split(".", 1)[1]
            source = self.signal_episodes_ddl if table_name == "intraday_signal_episodes" else SCHEMA_SQL + PLATFORM_SCHEMA_SQL
            block = _table_block(source, table_name)
            columns = re.findall(r"(?<![\w>'])([a-z_]+)(?= DESC|,|\))", definition.replace("USING brin ", ""))
            columns = [c for c in columns if c not in {"row_data"}] + (["row_data"] if "row_data" in definition else [])
            self.assertTrue(columns, f"{name}: no columns parsed from {definition}")
            for column in columns:
                self.assertRegex(block, rf"(?m)^\s*{column} ", f"{name}: column {column} missing on {table}")

    def test_downgrade_drops_every_index_concurrently(self):
        statements, autocommit = _statements(self.module.downgrade)
        self.assertEqual(statements, autocommit)
        self.assertEqual(
            statements,
            [f"DROP INDEX CONCURRENTLY IF EXISTS quant.{name}" for name, _, _ in reversed(self.module.INDEXES)],
        )

    def test_chain_position(self):
        self.assertEqual(self.module.revision, "20260902_0084")
        self.assertEqual(self.module.down_revision, "20260901_0083")


class RetentionPolicyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.module = _load(VERSIONS / "20260902_0085_retention_policies.py")
        self.statements, _ = _statements(self.module.upgrade)

    def test_policy_table_then_disabled_seeds_then_function(self):
        self.assertTrue(self.statements[0].startswith("CREATE TABLE IF NOT EXISTS quant.retention_policies ("))
        seeds = [sql for sql in self.statements if sql.startswith("INSERT INTO quant.retention_policies")]
        self.assertEqual(len(seeds), len(self.module.SEED_POLICIES))
        for sql in seeds:
            self.assertRegex(sql, r"VALUES \('\w+', '\w+', \d+, \d+, false\) ON CONFLICT \(table_name\) DO NOTHING$")
        function = self.statements[-1]
        self.assertTrue(function.startswith("CREATE OR REPLACE FUNCTION quant.apply_retention_policy(p_table_name text, p_batch_size integer DEFAULT NULL)"))
        self.assertIn("IF NOT policy.enabled THEN RAISE EXCEPTION", function)
        self.assertIn("LIMIT $2", function)
        self.assertIn("make_interval(days => policy.retention_days)", function)

    def test_seeded_time_columns_exist_on_their_tables(self):
        ddl = SCHEMA_SQL + PLATFORM_SCHEMA_SQL
        for table, column, days, batch in self.module.SEED_POLICIES:
            block = _table_block(ddl, table)
            self.assertRegex(block, rf"(?m)^\s*{column} timestamptz", f"{table}.{column}")
            self.assertGreater(days, 0)
            self.assertGreater(batch, 0)
        self.assertEqual({t for t, *_ in self.module.SEED_POLICIES}, {
            "market_bars_minute", "intraday_minute_sessions", "intraday_quote_observations", "tushare_raw_records",
        })

    def test_downgrade_removes_function_and_table(self):
        statements, _ = _statements(self.module.downgrade)
        self.assertEqual(statements, [
            "DROP FUNCTION IF EXISTS quant.apply_retention_policy(text, integer)",
            "DROP TABLE IF EXISTS quant.retention_policies",
        ])
        self.assertEqual(self.module.down_revision, "20260902_0084")


class FrozenLegacySchemaTests(unittest.TestCase):
    def test_legacy_ddl_is_frozen(self):
        self.assertEqual(hashlib.sha256(SCHEMA_SQL.encode("utf-8")).hexdigest(), FROZEN_SCHEMA_SQL_SHA256)
        self.assertEqual(hashlib.sha256(PLATFORM_SCHEMA_SQL.encode("utf-8")).hexdigest(), FROZEN_PLATFORM_SCHEMA_SQL_SHA256)

    def test_tables_defined_in_both_ddl_and_migrations_do_not_grow(self):
        ddl_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS quant\.(\w+)", SCHEMA_SQL + PLATFORM_SCHEMA_SQL))
        migration_tables = set()
        for path in VERSIONS.glob("*.py"):
            migration_tables |= set(re.findall(r"CREATE TABLE(?: IF NOT EXISTS)? quant\.(\w+)",
                                               path.read_text(encoding="utf-8")))
        self.assertEqual(ddl_tables & migration_tables, TABLES_IN_BOTH_DDL_AND_MIGRATIONS)
        self.assertEqual(
            len(re.findall(r"ALTER TABLE quant\.\w+ ADD COLUMN", SCHEMA_SQL + PLATFORM_SCHEMA_SQL)),
            LEGACY_DDL_ADD_COLUMN_COUNT,
        )

    def test_tables_migrations_alter_exist_somewhere(self):
        ddl_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS quant\.(\w+)", SCHEMA_SQL + PLATFORM_SCHEMA_SQL))
        created_so_far = set(ddl_tables)
        for path in sorted(VERSIONS.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            created_so_far |= set(re.findall(r"CREATE TABLE(?: IF NOT EXISTS)? quant\.(\w+)", source))
            for table in re.findall(r"ALTER TABLE quant\.(\w+)", source):
                self.assertIn(table, created_so_far, f"{path.name} alters unknown table quant.{table}")


if __name__ == "__main__":
    unittest.main()
