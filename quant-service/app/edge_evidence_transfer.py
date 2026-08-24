"""Bounded one-way transfer of intraday evidence from edge to research.

The edge remains the sole live-polling writer.  This module exposes a JSONL
stream for a restricted SSH command and imports that stream idempotently into
the workstation database.  It never transfers credentials, leases, alert
deliveries, recommendations, or order-like state.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable
from urllib.request import urlopen

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class TransferTable:
    name: str
    watermark_column: str
    conflict_columns: tuple[str, ...]
    json_columns: frozenset[str] = frozenset()


# Parent tables precede their dependants so one transaction preserves all FKs.
TRANSFER_TABLES: tuple[TransferTable, ...] = (
    TransferTable(
        "ten_day_leader_rotation_runs", "updated_at", ("run_id",),
        frozenset({"source_status", "summary"}),
    ),
    TransferTable(
        "ten_day_leader_rotation_candidates", "discovered_at", ("run_id", "symbol"),
        frozenset({"evidence", "reason_codes", "risk_flags", "source_snapshot"}),
    ),
    TransferTable(
        "intraday_scan_runs", "observed_at", ("scan_id",),
        frozenset({"requested_symbols", "source_status", "summary"}),
    ),
    TransferTable(
        "intraday_signal_episodes", "updated_at", ("episode_id",), frozenset({"evidence"}),
    ),
    TransferTable(
        "intraday_quote_observations", "observed_at", ("quote_observation_id",), frozenset({"raw"}),
    ),
    TransferTable(
        "intraday_minute_sessions", "available_at",
        ("symbol", "trading_date", "minute_bucket", "source_name"), frozenset({"raw"}),
    ),
    TransferTable(
        "intraday_board_flow_snapshots", "observed_at", ("flow_snapshot_id",),
        frozenset({"coverage", "source_status", "payload"}),
    ),
    TransferTable(
        "intraday_board_reports", "observed_at", ("board_report_id",),
        frozenset({"source_status", "summary", "payload"}),
    ),
    TransferTable(
        "intraday_board_rotation_events", "updated_at", ("rotation_event_id",),
        frozenset({"conditions"}),
    ),
    TransferTable(
        "intraday_signal_events", "observed_at", ("signal_event_id",),
        frozenset({"conditions", "evidence", "risk_flags"}),
    ),
    TransferTable(
        "intraday_rule_input_snapshots", "observed_at", ("rule_input_snapshot_id",),
        frozenset({"inputs"}),
    ),
    TransferTable(
        "ten_day_leader_rotation_intraday_observations", "observed_at", ("observation_id",),
        frozenset({"evidence", "reason_codes", "risk_flags", "source_snapshot"}),
    ),
)


def parse_checkpoint(value: str, *, now: datetime | None = None) -> datetime:
    """Validate one exact durable checkpoint without changing its meaning."""
    reference = now or datetime.now(timezone.utc)
    if not value.strip():
        return reference - timedelta(days=30)
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("edge evidence cursor must be timezone-aware")
    parsed = parsed.astimezone(timezone.utc)
    if parsed > reference + timedelta(minutes=5):
        raise ValueError("edge evidence cursor is in the future")
    return parsed


def parse_since(value: str, *, now: datetime | None = None) -> datetime:
    """Overlap a valid cursor and cap the remote scan window."""
    reference = now or datetime.now(timezone.utc)
    parsed = parse_checkpoint(value, now=reference)
    # The edge already has tighter per-table retention.  This protects a lost
    # cursor from turning an incremental handoff into an unbounded export.
    return max(parsed - timedelta(minutes=5), reference - timedelta(days=30))


def upsert_statement(table: TransferTable, columns: tuple[str, ...]) -> str:
    allowed = {column for column in columns}
    if (
        not columns
        or not set(table.conflict_columns).issubset(allowed)
        or any(re.fullmatch(r"[a-z_][a-z0-9_]*", column) is None for column in columns)
    ):
        raise ValueError(f"invalid columns for {table.name}")
    quoted = ",".join(f'"{column}"' for column in columns)
    placeholders = ",".join(["%s"] * len(columns))
    conflicts = ",".join(f'"{column}"' for column in table.conflict_columns)
    updates = [column for column in columns if column not in table.conflict_columns]
    if updates:
        action = "DO UPDATE SET " + ",".join(
            f'"{column}"=EXCLUDED."{column}"' for column in updates
        )
    else:
        action = "DO NOTHING"
    return (
        f'INSERT INTO quant."{table.name}" ({quoted}) VALUES ({placeholders}) '
        f"ON CONFLICT ({conflicts}) {action}"
    )


def _json_default(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def edge_runtime_snapshot() -> dict[str, Any]:
    """Read only secret-free loopback health fields for the local status UI."""
    try:
        with urlopen("http://127.0.0.1:18110/health", timeout=3) as response:
            health = json.load(response)
        with urlopen("http://127.0.0.1:18110/api/v1/intraday/services/status", timeout=3) as response:
            intraday = json.load(response)
    except Exception as error:  # the evidence stream remains usable without the UI snapshot
        return {"status": "unavailable", "error": str(error)[:300]}
    resources = health.get("resources") if isinstance(health, dict) else {}
    disk = resources.get("disk") if isinstance(resources, dict) else {}
    storage = resources.get("research_storage") if isinstance(resources, dict) else {}
    return {
        "status": str(health.get("status") or "unknown"),
        "runtime_profile": (health.get("optional_background_tasks") or {}).get("runtime_profile"),
        "runtime_loops": health.get("runtime_loops") or {},
        "daily_control_plane": health.get("daily_control_plane") or {},
        "resources": {
            "state": resources.get("state") if isinstance(resources, dict) else None,
            "disk_free_bytes": disk.get("free_bytes") if isinstance(disk, dict) else None,
            "hot_database": storage.get("hot_database") if isinstance(storage, dict) else None,
            "managed": storage.get("managed") if isinstance(storage, dict) else None,
        },
        "intraday": {
            "observed_at": intraday.get("observed_at"),
            "session_active": intraday.get("session_active"),
            "session_reason": intraday.get("session_reason"),
            "summary": intraday.get("summary") or {},
            "items": intraday.get("items") or [],
        },
    }


def export_jsonl(since: datetime, output: Any = sys.stdout) -> dict[str, Any]:
    """Write one repeatable-read, bounded evidence snapshot as JSONL."""
    emitted: dict[str, int] = {}
    with psycopg.connect(row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        checkpoint = connection.execute("SELECT transaction_timestamp() AS value").fetchone()["value"]
        print(json.dumps({
            "kind": "metadata", "version": 1, "since": since.isoformat(),
            "checkpoint": checkpoint.isoformat(), "scope": "research_evidence_only",
            "edge_runtime": edge_runtime_snapshot(),
        }, ensure_ascii=False), file=output)
        for table in TRANSFER_TABLES:
            rows = connection.execute(
                f'SELECT * FROM quant."{table.name}" WHERE "{table.watermark_column}">%s '
                f'AND "{table.watermark_column}"<=%s ORDER BY "{table.watermark_column}"',
                (since, checkpoint),
            )
            count = 0
            for row in rows:
                print(json.dumps(
                    {"kind": "record", "table": table.name, "row": dict(row)},
                    ensure_ascii=False, default=_json_default, separators=(",", ":"),
                ), file=output)
                count += 1
            emitted[table.name] = count
        print(json.dumps({
            "kind": "checkpoint", "version": 1, "value": checkpoint.isoformat(),
            "counts": emitted,
        }, ensure_ascii=False, separators=(",", ":")), file=output)
    return {"checkpoint": checkpoint.isoformat(), "counts": emitted}


def _cursor_path() -> Path:
    return Path(os.getenv(
        "QUANT_EDGE_EVIDENCE_CURSOR_PATH", "/var/lib/quant/edge-evidence-cursor.json",
    ))


def read_cursor(path: Path | None = None) -> str:
    cursor_path = path or _cursor_path()
    try:
        payload = json.loads(cursor_path.read_text(encoding="utf-8"))
        value = str(payload.get("checkpoint") or "")
        return parse_checkpoint(value).isoformat()
    except FileNotFoundError:
        return parse_checkpoint("").isoformat()
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid edge evidence cursor: {error}") from error


def _write_cursor(
    checkpoint: str, counts: dict[str, int], edge_runtime: dict[str, Any], path: Path | None = None,
) -> None:
    cursor_path = path or _cursor_path()
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cursor_path.with_suffix(cursor_path.suffix + ".tmp")
    temporary.write_text(json.dumps({
        "checkpoint": checkpoint, "counts": counts,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "edge_runtime": edge_runtime,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(cursor_path)


def import_jsonl(lines: Iterable[str], *, cursor_path: Path | None = None) -> dict[str, Any]:
    """Apply a complete stream atomically and advance its cursor after commit."""
    by_name = {table.name: table for table in TRANSFER_TABLES}
    counts = {table.name: 0 for table in TRANSFER_TABLES}
    checkpoint = ""
    metadata_seen = False
    edge_runtime: dict[str, Any] = {}
    with psycopg.connect() as connection:
        with connection.transaction():
            for raw_line in lines:
                if not raw_line.strip():
                    continue
                item = json.loads(raw_line)
                kind = item.get("kind")
                if kind == "metadata":
                    if metadata_seen or int(item.get("version") or 0) != 1:
                        raise ValueError("invalid edge evidence metadata")
                    metadata_seen = True
                    edge_runtime = item.get("edge_runtime") if isinstance(item.get("edge_runtime"), dict) else {}
                    continue
                if kind == "checkpoint":
                    checkpoint = parse_checkpoint(str(item.get("value") or "")).isoformat()
                    continue
                if kind != "record" or not metadata_seen or checkpoint:
                    raise ValueError("invalid edge evidence stream ordering")
                table = by_name.get(str(item.get("table") or ""))
                row = item.get("row")
                if table is None or not isinstance(row, dict):
                    raise ValueError("unknown edge evidence table or row")
                columns = tuple(row.keys())
                statement = upsert_statement(table, columns)
                values = tuple(
                    Jsonb(row[column]) if column in table.json_columns and row[column] is not None else row[column]
                    for column in columns
                )
                connection.execute(statement, values)
                counts[table.name] += 1
    if not metadata_seen or not checkpoint:
        raise ValueError("edge evidence stream is incomplete")
    _write_cursor(checkpoint, counts, edge_runtime, cursor_path)
    return {"status": "completed", "checkpoint": checkpoint, "counts": counts}


def edge_evidence_status(
    path: Path | None = None, *, now: datetime | None = None, stale_after_seconds: int = 1800,
) -> dict[str, Any]:
    """Project the local cursor and remote snapshot without network I/O."""
    cursor_path = path or _cursor_path()
    try:
        payload = json.loads(cursor_path.read_text(encoding="utf-8"))
        imported_at = parse_checkpoint(str(payload.get("imported_at") or ""), now=now)
        observed_at = now or datetime.now(timezone.utc)
        age_seconds = max(0.0, (observed_at - imported_at).total_seconds())
        runtime = payload.get("edge_runtime") if isinstance(payload.get("edge_runtime"), dict) else {}
        remote_ok = runtime.get("status") == "ok" and runtime.get("runtime_profile") == "intraday_edge"
        state = "ready" if remote_ok and age_seconds <= stale_after_seconds else "stale" if runtime else "unavailable"
        return {
            "configured": True, "state": state, "last_imported_at": imported_at.isoformat(),
            "age_seconds": round(age_seconds, 1), "stale_after_seconds": stale_after_seconds,
            "checkpoint": payload.get("checkpoint"), "counts": payload.get("counts") or {},
            "runtime": runtime,
        }
    except FileNotFoundError:
        return {"configured": False, "state": "disabled", "runtime": {}}
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        return {"configured": True, "state": "unavailable", "error": str(error)[:300], "runtime": {}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transfer bounded intraday edge evidence")
    subcommands = parser.add_subparsers(dest="command", required=True)
    export_parser = subcommands.add_parser("export")
    export_parser.add_argument("--since", default="")
    subcommands.add_parser("import")
    subcommands.add_parser("cursor")
    arguments = parser.parse_args(argv)
    if arguments.command == "cursor":
        print(read_cursor())
        return 0
    if arguments.command == "export":
        export_jsonl(parse_since(arguments.since))
        return 0
    result = import_jsonl(sys.stdin)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "TRANSFER_TABLES", "TransferTable", "edge_evidence_status", "edge_runtime_snapshot",
    "export_jsonl", "import_jsonl", "parse_checkpoint", "parse_since", "read_cursor", "upsert_statement",
]
