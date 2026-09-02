"""Run versioned schema migrations safely before starting the API process."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import psycopg

from app import db_dsn


# This is a fixed application-level lock namespace, not derived from a secret.
# Every quant-research instance must hold it while applying Alembic revisions.
MIGRATION_ADVISORY_LOCK_KEY = 7_265_811_000_001


def migration_lock_timeout_seconds() -> int:
    raw = os.getenv("QUANT_MIGRATION_LOCK_TIMEOUT_SECONDS", "60")
    try:
        return max(1, min(int(raw), 600))
    except ValueError:
        return 60


def database_connection() -> psycopg.Connection:
    params = db_dsn.connection_params()
    return psycopg.connect(
        host=params["host"],
        port=params["port"],
        dbname=params["dbname"],
        user=params["user"],
        password=params["password"],
        connect_timeout=10,
        autocommit=True,
    )


def acquire_migration_lock(connection: psycopg.Connection) -> None:
    deadline = time.monotonic() + migration_lock_timeout_seconds()
    while True:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,))
            acquired = bool(cursor.fetchone()[0])
        if acquired:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("timed out waiting for the quant schema migration lock")
        time.sleep(1)


def release_migration_lock(connection: psycopg.Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,))


def migration_command() -> list[str]:
    """Run Alembic from the same virtual environment as the service."""
    return [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"]


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit("usage: entrypoint.py <service command>")

    connection = database_connection()
    try:
        acquire_migration_lock(connection)
        print("applying versioned quant schema migrations", flush=True)
        subprocess.run(migration_command(), check=True)
    finally:
        try:
            release_migration_lock(connection)
        finally:
            connection.close()

    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
