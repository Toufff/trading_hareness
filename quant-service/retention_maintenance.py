"""Operator command: delete expired rows for enabled retention policies.

Policies live in ``quant.retention_policies`` (migration 20260902_0085) and are
disabled by default.  This command loops ``quant.apply_retention_policy`` in
bounded batches on an autocommit connection so each batch commits on its own
and never holds a long transaction on a hot table.  It is not scheduled; see
the WP7 report for the task-registry hook.

Usage:
    python retention_maintenance.py [--table NAME] [--max-batches N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from entrypoint import database_connection


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--table", default=None, help="only this quant.<table> policy")
    parser.add_argument("--max-batches", type=int, default=50, help="upper bound of delete batches per table")
    parser.add_argument("--dry-run", action="store_true", help="list enabled policies and expired-row counts only")
    return parser.parse_args(argv)


def load_policies(connection, table: str | None) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name, time_column, retention_days, batch_size, enabled "
            "FROM quant.retention_policies WHERE (%s::text IS NULL OR table_name = %s) ORDER BY table_name",
            (table, table),
        )
        return [
            {"table_name": row[0], "time_column": row[1], "retention_days": int(row[2]),
             "batch_size": int(row[3]), "enabled": bool(row[4])}
            for row in cursor.fetchall()
        ]


def expired_row_count(connection, policy: dict[str, Any]) -> int:
    # Identifiers come from the policy table, whose CHECK constraints restrict
    # them to plain lower-case identifiers.
    query = (
        f"SELECT count(*)::bigint FROM quant.{policy['table_name']} "
        f"WHERE {policy['time_column']} < now() - make_interval(days => %s)"
    )
    with connection.cursor() as cursor:
        cursor.execute(query, (policy["retention_days"],))
        return int(cursor.fetchone()[0])


def apply_policy(connection, table_name: str, *, max_batches: int) -> dict[str, Any]:
    deleted = 0
    batches = 0
    complete = False
    while batches < max_batches:
        with connection.cursor() as cursor:
            cursor.execute("SELECT deleted_rows FROM quant.apply_retention_policy(%s)", (table_name,))
            removed = int(cursor.fetchone()[0])
        batches += 1
        deleted += removed
        if removed == 0:
            complete = True
            break
    return {"table_name": table_name, "deleted_rows": deleted, "batches": batches, "complete": complete}


def run(argv: list[str], *, connection=None) -> dict[str, Any]:
    args = parse_args(argv)
    if args.max_batches <= 0:
        raise SystemExit("--max-batches must be positive")
    own_connection = connection is None
    if own_connection:
        connection = database_connection()
    try:
        policies = load_policies(connection, args.table)
        if args.table and not policies:
            raise SystemExit(f"no retention policy for quant.{args.table}")
        results: list[dict[str, Any]] = []
        for policy in policies:
            if not policy["enabled"]:
                results.append({**policy, "status": "disabled"})
                continue
            if args.dry_run:
                results.append({**policy, "status": "dry_run", "expired_rows": expired_row_count(connection, policy)})
                continue
            results.append({**policy, "status": "applied",
                            **apply_policy(connection, policy["table_name"], max_batches=args.max_batches)})
        return {"status": "ok", "dry_run": bool(args.dry_run), "policies": results}
    finally:
        if own_connection:
            connection.close()


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1:]), ensure_ascii=False, default=str))
