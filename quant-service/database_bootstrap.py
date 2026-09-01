"""Explicit, resumable initialization for a genuinely empty PostgreSQL DB.

Production startup remains migration-only.  This operator command exists for
new installations and interrupted first-run recovery: it creates the frozen
legacy baseline once, stamps that exact baseline revision, and then lets every
subsequent schema change flow through Alembic.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Literal

from app.database import PLATFORM_SCHEMA_SQL, SCHEMA_SQL
from entrypoint import acquire_migration_lock, database_connection, release_migration_lock


BASELINE_REVISION = "20260811_0001"
REQUIRED_BASELINE_TABLES = (
    "quant.instruments",
    "quant.market_bars_daily",
    "quant.raw_market_observations",
    "quant.canonical_bars_daily",
    "quant.providers",
    "quant.fetch_runs",
)

# The frozen baseline accumulated two later sector-flow tables before the
# original taxonomy tables they reference. Existing databases masked that
# historical ordering, while a truly empty database cannot. These exact
# prerequisite definitions run in the same transaction as the frozen DDL; they
# are not a second evolving production schema.
BASELINE_PREREQUISITES_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS quant;
CREATE TABLE IF NOT EXISTS quant.providers (
    provider_key text PRIMARY KEY,
    label text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    config jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS quant.sector_taxonomies (
    taxonomy_key text PRIMARY KEY,
    label text NOT NULL,
    provider_key text NOT NULL REFERENCES quant.providers(provider_key) ON DELETE RESTRICT,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS quant.sectors (
    taxonomy_key text NOT NULL REFERENCES quant.sector_taxonomies(taxonomy_key) ON DELETE CASCADE,
    sector_key text NOT NULL,
    label text NOT NULL,
    parent_sector_key text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(taxonomy_key, sector_key)
);
"""


def bootstrap_action(
    *,
    version_table_present: bool,
    quant_table_count: int,
    required_table_count: int,
) -> Literal["upgrade", "create_baseline", "stamp_existing"]:
    """Choose a safe first-run action without guessing through partial DDL."""
    if version_table_present:
        return "upgrade"
    if quant_table_count == 0:
        return "create_baseline"
    if required_table_count == len(REQUIRED_BASELINE_TABLES):
        return "stamp_existing"
    raise RuntimeError(
        "unversioned quant schema is partial; restore/drop the failed fresh database "
        "instead of stamping an unknown state"
    )


def _schema_state(connection) -> tuple[bool, int, int]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('quant.alembic_version') IS NOT NULL")
        version_table_present = bool(cursor.fetchone()[0])
        cursor.execute("SELECT count(*)::int FROM pg_tables WHERE schemaname='quant'")
        quant_table_count = int(cursor.fetchone()[0])
        cursor.execute(
            "SELECT count(*)::int FROM unnest(%s::text[]) AS name "
            "WHERE to_regclass(name) IS NOT NULL",
            (list(REQUIRED_BASELINE_TABLES),),
        )
        required_table_count = int(cursor.fetchone()[0])
    return version_table_present, quant_table_count, required_table_count


def _run_alembic(*args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=Path(__file__).resolve().parent,
        check=True,
    )


def initialize_database() -> dict[str, str]:
    connection = database_connection()
    try:
        acquire_migration_lock(connection)
        state = _schema_state(connection)
        action = bootstrap_action(
            version_table_present=state[0],
            quant_table_count=state[1],
            required_table_count=state[2],
        )
        if action == "create_baseline":
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.ingestion_jobs') IS NOT NULL")
                if not bool(cursor.fetchone()[0]):
                    raise RuntimeError(
                        "public ingestion ledger is absent; run "
                        "feishu-adapter/initialize-ledger.mjs before the quant bootstrap"
                    )
            # Keep the prerequisites and frozen baseline atomic. A failed
            # empty-DB bootstrap therefore leaves no partial quant schema that
            # a later run might accidentally stamp as valid.
            with connection.transaction():
                connection.execute(BASELINE_PREREQUISITES_SQL)
                connection.execute(SCHEMA_SQL + PLATFORM_SCHEMA_SQL)

        if action in {"create_baseline", "stamp_existing"}:
            current = _schema_state(connection)
            if current[2] != len(REQUIRED_BASELINE_TABLES):
                raise RuntimeError("legacy baseline did not create every required table")
            _run_alembic("stamp", BASELINE_REVISION)

        _run_alembic("upgrade", "head")
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM quant.alembic_version")
            revision = str(cursor.fetchone()[0])
        return {"status": "ready", "action": action, "revision": revision}
    finally:
        try:
            release_migration_lock(connection)
        finally:
            connection.close()


if __name__ == "__main__":
    print(json.dumps(initialize_database(), ensure_ascii=False))
