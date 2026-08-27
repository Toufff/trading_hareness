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
    # The strategy runs on the edge; without this its observations never reach
    # the workstation where outcomes are settled and modes are scored.
    # last_seen_at is the watermark because a held setup is one row whose
    # window widens, so it advances on every re-observation.
    TransferTable(
        "xiaojie_leader_flow_observations", "last_seen_at", ("trading_date", "symbol", "mode"),
        frozenset({"stop_loss", "exit_state", "risk_flags", "reasons", "market_gate",
                   "first_evidence", "last_evidence"}),
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

CHANGE_PAGE_SIZE = 5_000
# PostgreSQL sequences are allocated before commit. Replaying this bounded
# tail prevents a short transaction that commits after a later sequence from
# being skipped when a workstation advances its durable cursor.
# It must remain smaller than one page: a page reserves room for new rows,
# otherwise a cursor at the end of a quiet stream would export only its replay
# tail forever and never advance through a subsequent backlog.
CHANGE_REPLAY_WINDOW = 1_000


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


def parse_sequence(value: str | int | None) -> int:
    """Validate the monotonic edge journal position without accepting floats."""
    if value is None or str(value).strip() == "":
        return 0
    if not re.fullmatch(r"[0-9]+", str(value).strip()):
        raise ValueError("edge evidence sequence must be a non-negative integer")
    return int(str(value).strip())


def parse_restricted_export_command(value: str) -> tuple[str, str | int]:
    """Validate the single command accepted by the edge's forced SSH key.

    The SSH server provides the complete client command as one string through
    ``SSH_ORIGINAL_COMMAND``.  Keep the protocol parser here, next to the
    timestamp and sequence validators, so a shell-regex change cannot silently
    disagree with the JSONL exporter.  Both RFC 3339 ``Z`` and explicit UTC
    offsets are accepted and normalized before the database read starts.
    """
    command, separator, argument = str(value or "").strip().partition(" ")
    argument = argument.strip()
    if not separator or not argument:
        raise ValueError("usage: export-since ISO8601 | export-changes SEQUENCE")
    if command == "export-since":
        return command, parse_checkpoint(argument).isoformat()
    if command == "export-changes":
        return command, parse_sequence(argument)
    raise ValueError("usage: export-since ISO8601 | export-changes SEQUENCE")


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
    except Exception as error:  # the evidence stream remains usable without the UI snapshot
        return {"status": "unavailable", "error": str(error)[:300]}
    resources = health.get("resources") if isinstance(health, dict) else {}
    disk = resources.get("disk") if isinstance(resources, dict) else {}
    storage = resources.get("research_storage") if isinstance(resources, dict) else {}
    automation = health.get("intraday_automation") if isinstance(health, dict) else {}
    automation = automation if isinstance(automation, dict) else {}
    return {
        "status": str(health.get("status") or "unknown"),
        "runtime_profile": (health.get("optional_background_tasks") or {}).get("runtime_profile"),
        # Health already exposes this as safe, validated release metadata. Keep
        # it with the imported snapshot so the research UI can tie every
        # evidence cursor to the exact remote collector revision.
        "build": health.get("build") if isinstance(health.get("build"), dict) else {},
        "runtime_loops": health.get("runtime_loops") or {},
        "daily_control_plane": health.get("daily_control_plane") or {},
        "live_session_acceptance": health.get("live_session_acceptance")
        if isinstance(health.get("live_session_acceptance"), dict) else {"state": "unavailable"},
        "resources": {
            "state": resources.get("state") if isinstance(resources, dict) else None,
            "disk_free_bytes": disk.get("free_bytes") if isinstance(disk, dict) else None,
            "disk_warning_free_bytes": disk.get("warning_free_bytes") if isinstance(disk, dict) else None,
            "disk_min_free_bytes": disk.get("min_free_bytes") if isinstance(disk, dict) else None,
            "hot_database": storage.get("hot_database") if isinstance(storage, dict) else None,
            "managed": storage.get("managed") if isinstance(storage, dict) else None,
        },
        "intraday": {
            # The full services status can include a large strategy-detail
            # payload during active sessions. It is intentionally not part of
            # every evidence export: a slow dashboard read must never block
            # the collector-to-research data handoff.
            "session_active": automation.get("session_active"),
            "session_reason": automation.get("session_reason"),
            "summary": {},
            "items": [],
        },
    }


def _journal_checkpoint(connection: psycopg.Connection) -> int:
    exists = connection.execute("SELECT to_regclass('quant.edge_evidence_changes') AS value").fetchone()["value"]
    if not exists:
        return 0
    row = connection.execute("SELECT coalesce(max(sequence_id), 0)::bigint AS value FROM quant.edge_evidence_changes").fetchone()
    return int(row["value"] or 0)


def _journal_head(connection: psycopg.Connection) -> tuple[int, datetime | None]:
    """Return one point-in-time journal head for the incremental handoff."""
    row = connection.execute(
        """SELECT coalesce(max(sequence_id), 0)::bigint AS sequence,
                  max(changed_at) AS latest_changed_at
             FROM quant.edge_evidence_changes"""
    ).fetchone()
    return int(row["sequence"] or 0), row["latest_changed_at"]


def export_jsonl(since: datetime, output: Any = sys.stdout) -> dict[str, Any]:
    """Write one repeatable-read, bounded evidence snapshot as JSONL."""
    emitted: dict[str, int] = {}
    with psycopg.connect(row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        checkpoint = connection.execute("SELECT transaction_timestamp() AS value").fetchone()["value"]
        journal_sequence = _journal_checkpoint(connection)
        print(json.dumps({
            "kind": "metadata", "version": 1, "since": since.isoformat(),
            "checkpoint": checkpoint.isoformat(), "journal_sequence": journal_sequence,
            "scope": "research_evidence_only",
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
            "counts": emitted, "journal_sequence": journal_sequence,
        }, ensure_ascii=False, separators=(",", ":")), file=output)
    return {"checkpoint": checkpoint.isoformat(), "counts": emitted, "journal_sequence": journal_sequence}


def export_changes(
    after_sequence: int,
    *,
    limit: int = CHANGE_PAGE_SIZE,
    output: Any = sys.stdout,
) -> dict[str, Any]:
    """Export one bounded, replay-safe page from the edge change journal."""
    requested_after = parse_sequence(after_sequence)
    bounded_limit = max(1, min(CHANGE_PAGE_SIZE, int(limit)))
    replay_from = max(0, requested_after - CHANGE_REPLAY_WINDOW)
    with psycopg.connect(row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        checkpoint = connection.execute("SELECT transaction_timestamp() AS value").fetchone()["value"]
        if not connection.execute("SELECT to_regclass('quant.edge_evidence_changes') AS value").fetchone()["value"]:
            raise RuntimeError("edge evidence change journal is unavailable; run the current Alembic migration first")
        remote_sequence, remote_latest_changed_at = _journal_head(connection)
        rows = connection.execute(
            """SELECT sequence_id, table_name, record_key, row_data, changed_at
                 FROM quant.edge_evidence_changes
                WHERE sequence_id > %s
                ORDER BY sequence_id
                LIMIT %s""",
            (replay_from, bounded_limit),
        ).fetchall()
        next_sequence = max((int(row["sequence_id"]) for row in rows), default=requested_after)
        print(json.dumps({
            "kind": "metadata", "version": 2, "mode": "change_journal",
            "requested_after_sequence": requested_after, "replay_from_sequence": replay_from,
            "remote_sequence": remote_sequence,
            "remote_latest_changed_at": remote_latest_changed_at.isoformat() if remote_latest_changed_at else None,
            "checkpoint": checkpoint.isoformat(), "scope": "research_evidence_only",
            "edge_runtime": edge_runtime_snapshot(),
        }, ensure_ascii=False), file=output)
        counts = {table.name: 0 for table in TRANSFER_TABLES}
        for row in rows:
            table_name = str(row["table_name"])
            if table_name not in counts:
                raise RuntimeError(f"edge evidence journal contains unsupported table: {table_name}")
            print(json.dumps({
                "kind": "record", "table": table_name, "row": row["row_data"],
                "sequence_id": int(row["sequence_id"]), "record_key": row["record_key"],
            }, ensure_ascii=False, default=_json_default, separators=(",", ":")), file=output)
            counts[table_name] += 1
        print(json.dumps({
            "kind": "checkpoint", "version": 2, "value": checkpoint.isoformat(),
            "sequence": next_sequence, "counts": counts,
            "remote_sequence": remote_sequence,
            "remote_latest_changed_at": remote_latest_changed_at.isoformat() if remote_latest_changed_at else None,
            "has_more": len(rows) >= bounded_limit,
        }, ensure_ascii=False, separators=(",", ":")), file=output)
    return {"checkpoint": checkpoint.isoformat(), "sequence": next_sequence, "counts": counts,
            "remote_sequence": remote_sequence,
            "remote_latest_changed_at": remote_latest_changed_at.isoformat() if remote_latest_changed_at else None,
            "has_more": len(rows) >= bounded_limit}


def _cursor_path() -> Path:
    return Path(os.getenv(
        "QUANT_EDGE_EVIDENCE_CURSOR_PATH", "/var/lib/quant/edge-evidence-cursor.json",
    ))


def _pull_status_path() -> Path:
    return Path(os.getenv(
        "QUANT_EDGE_EVIDENCE_PULL_STATUS_PATH", "/var/lib/quant/edge-evidence-pull-status.json",
    ))


def _live_session_acceptance_path() -> Path:
    configured = os.getenv("QUANT_EDGE_LIVE_SESSION_ACCEPTANCE_PATH", "").strip()
    if configured:
        return Path(configured)
    return Path(os.getenv("QUANT_DATA_DIR", "/var/lib/quant")) / "live-session-acceptance.json"


def read_live_session_acceptance(path: Path | None = None) -> dict[str, Any]:
    """Read the edge-owned, secret-free last market-session acceptance result."""
    acceptance_path = path or _live_session_acceptance_path()
    try:
        payload = json.loads(acceptance_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"state": "not_run"}
    except (TypeError, json.JSONDecodeError) as error:
        return {"state": "unavailable", "reason": f"invalid live-session acceptance: {str(error)[:240]}"}
    if not isinstance(payload, dict):
        return {"state": "unavailable", "reason": "invalid live-session acceptance payload"}
    state = str(payload.get("state") or "unavailable")
    if state not in {"passed", "failed", "standby"}:
        return {"state": "unavailable", "reason": "invalid live-session acceptance state"}
    return payload


def write_live_session_acceptance(payload: dict[str, Any], *, path: Path | None = None) -> dict[str, Any]:
    """Atomically persist one acceptance outcome without tokens or source payloads."""
    acceptance_path = path or _live_session_acceptance_path()
    acceptance_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = acceptance_path.with_suffix(acceptance_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(acceptance_path)
    return read_live_session_acceptance(acceptance_path)


def assess_live_session_acceptance(health: dict[str, Any], status: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Evaluate only the active market-data loops from the published status."""
    checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    build = health.get("build") if isinstance(health.get("build"), dict) else {}
    profile = (health.get("optional_background_tasks") or {}).get("runtime_profile")
    session_active = status.get("session_active") is True
    items = status.get("items") if isinstance(status.get("items"), list) else []
    required = [
        item for item in items
        if isinstance(item, dict) and item.get("expected_active") is True and item.get("key") != "feishu_alert"
    ]
    safe_items = [{
        "key": item.get("key"), "state": item.get("state"),
        "last_observed_at": item.get("last_observed_at"), "age_seconds": item.get("age_seconds"),
        "max_age_seconds": item.get("max_age_seconds"), "last_error": item.get("last_error"),
    } for item in required]
    result: dict[str, Any] = {
        "checked_at": checked_at, "build": build, "session_active": session_active,
        "session_reason": str(status.get("session_reason") or ""), "items": safe_items,
    }
    if health.get("status") != "ok" or profile != "intraday_edge":
        return {**result, "state": "failed", "reason": "edge health or runtime profile is invalid"}
    if not session_active:
        return {**result, "state": "standby", "reason": "SSE continuous session is inactive"}
    if not required:
        return {**result, "state": "failed", "reason": "no market-data loop is marked expected_active"}
    failed = [item for item in required if not (
        item.get("state") == "healthy"
        and item.get("last_observed_at") is not None
        and item.get("last_error") is None
        and isinstance(item.get("age_seconds"), (int, float))
        and isinstance(item.get("max_age_seconds"), (int, float))
        and item["age_seconds"] <= item["max_age_seconds"]
    )]
    if failed:
        return {**result, "state": "failed", "reason": "one or more expected market-data loops are stale or unhealthy"}
    return {**result, "state": "passed", "reason": "all expected market-data loops are fresh"}


def run_live_session_acceptance(*, path: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Read loopback status and persist a bounded result for a systemd timer."""
    try:
        with urlopen("http://127.0.0.1:18110/health", timeout=3) as response:
            health = json.load(response)
        with urlopen("http://127.0.0.1:18110/api/v1/intraday/services/status", timeout=3) as response:
            status = json.load(response)
        if not isinstance(health, dict) or not isinstance(status, dict):
            raise ValueError("edge health response is invalid")
        result = assess_live_session_acceptance(health, status, now=now)
    except Exception as error:  # leave durable, secret-free evidence for the operator
        result = {
            "state": "failed", "checked_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
            "session_active": None, "items": [], "reason": f"live-session acceptance request failed: {str(error)[:240]}",
        }
    return write_live_session_acceptance(result, path=path)


def read_pull_status(path: Path | None = None) -> dict[str, Any]:
    """Read the workstation-owned pull attempt status without any network I/O."""
    status_path = path or _pull_status_path()
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (TypeError, json.JSONDecodeError) as error:
        return {"state": "unavailable", "last_error": f"invalid pull status: {str(error)[:240]}"}
    if not isinstance(payload, dict):
        return {"state": "unavailable", "last_error": "invalid pull status payload"}
    result: dict[str, Any] = {"state": str(payload.get("state") or "unknown")[:40]}
    for key in ("last_attempt_at", "last_success_at"):
        value = str(payload.get(key) or "").strip()
        if value:
            try:
                result[key] = parse_checkpoint(value).isoformat()
            except ValueError:
                result["state"] = "unavailable"
                result["last_error"] = f"invalid {key} in pull status"
                return result
    error = str(payload.get("last_error") or "").strip()
    if error:
        result["last_error"] = error[:300]
    for key in ("pages_imported", "rows_imported", "duration_ms"):
        value = payload.get(key)
        if isinstance(value, int) and value >= 0:
            result[key] = value
    return result


def write_pull_status(
    state: str, *, error: str | None = None, pages_imported: int | None = None,
    rows_imported: int | None = None, duration_ms: int | None = None, path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Atomically persist one local pull attempt outcome for the dashboard."""
    if state not in {"running", "catching_up", "completed", "failed"}:
        raise ValueError("invalid edge evidence pull state")
    status_path = path or _pull_status_path()
    previous = read_pull_status(status_path)
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "state": state,
        "last_attempt_at": observed_at,
        "last_success_at": previous.get("last_success_at"),
        "last_error": None,
    }
    if state in {"catching_up", "completed"}:
        payload["last_success_at"] = observed_at
    elif state == "failed":
        payload["last_error"] = str(error or "edge evidence pull failed").strip()[:300]
    for key, value in {
        "pages_imported": pages_imported,
        "rows_imported": rows_imported,
        "duration_ms": duration_ms,
    }.items():
        if value is not None:
            payload[key] = max(0, int(value))
    status_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = status_path.with_suffix(status_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(status_path)
    return read_pull_status(status_path)


def read_cursor(path: Path | None = None) -> str:
    return str(read_cursor_payload(path).get("checkpoint") or "")


def read_cursor_payload(path: Path | None = None) -> dict[str, Any]:
    cursor_path = path or _cursor_path()
    try:
        payload = json.loads(cursor_path.read_text(encoding="utf-8"))
        value = str(payload.get("checkpoint") or "")
        payload["checkpoint"] = parse_checkpoint(value).isoformat()
        payload["sequence"] = parse_sequence(payload.get("sequence"))
        payload["remote_sequence"] = (
            parse_sequence(payload.get("remote_sequence"))
            if "remote_sequence" in payload else payload["sequence"]
        )
        payload["has_more"] = bool(payload.get("has_more"))
        latest_changed_at = payload.get("remote_latest_changed_at")
        if latest_changed_at:
            payload["remote_latest_changed_at"] = parse_checkpoint(str(latest_changed_at)).isoformat()
        return payload
    except FileNotFoundError:
        return {"checkpoint": parse_checkpoint("").isoformat(), "sequence": 0}
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid edge evidence cursor: {error}") from error


def _write_cursor(
    checkpoint: str, counts: dict[str, int], edge_runtime: dict[str, Any], *, sequence: int = 0,
    remote_sequence: int = 0, remote_latest_changed_at: str | None = None, has_more: bool = False,
    path: Path | None = None,
) -> None:
    cursor_path = path or _cursor_path()
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cursor_path.with_suffix(cursor_path.suffix + ".tmp")
    temporary.write_text(json.dumps({
        "checkpoint": checkpoint, "counts": counts,
        "sequence": parse_sequence(sequence),
        "remote_sequence": parse_sequence(remote_sequence),
        "remote_latest_changed_at": remote_latest_changed_at,
        "has_more": bool(has_more),
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "edge_runtime": edge_runtime,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(cursor_path)


def import_jsonl(lines: Iterable[str], *, cursor_path: Path | None = None) -> dict[str, Any]:
    """Apply a complete stream atomically and advance its cursor after commit."""
    by_name = {table.name: table for table in TRANSFER_TABLES}
    counts = {table.name: 0 for table in TRANSFER_TABLES}
    checkpoint = ""
    sequence = 0
    remote_sequence = 0
    remote_latest_changed_at: str | None = None
    has_more = False
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
                    if metadata_seen or int(item.get("version") or 0) not in {1, 2}:
                        raise ValueError("invalid edge evidence metadata")
                    metadata_seen = True
                    edge_runtime = item.get("edge_runtime") if isinstance(item.get("edge_runtime"), dict) else {}
                    remote_sequence = parse_sequence(item.get("remote_sequence") or item.get("journal_sequence"))
                    remote_latest = item.get("remote_latest_changed_at")
                    if remote_latest:
                        remote_latest_changed_at = parse_checkpoint(str(remote_latest)).isoformat()
                    continue
                if kind == "checkpoint":
                    checkpoint = parse_checkpoint(str(item.get("value") or "")).isoformat()
                    sequence = parse_sequence(item.get("sequence") or item.get("journal_sequence"))
                    remote_sequence = parse_sequence(item.get("remote_sequence") or remote_sequence or sequence)
                    remote_latest = item.get("remote_latest_changed_at")
                    if remote_latest:
                        remote_latest_changed_at = parse_checkpoint(str(remote_latest)).isoformat()
                    has_more = bool(item.get("has_more"))
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
    _write_cursor(
        checkpoint, counts, edge_runtime, sequence=sequence, remote_sequence=remote_sequence,
        remote_latest_changed_at=remote_latest_changed_at, has_more=has_more, path=cursor_path,
    )
    return {
        "status": "completed", "checkpoint": checkpoint, "sequence": sequence,
        "remote_sequence": remote_sequence, "remote_latest_changed_at": remote_latest_changed_at,
        "has_more": has_more, "counts": counts,
    }


def edge_evidence_status(
    path: Path | None = None, *, now: datetime | None = None, stale_after_seconds: int = 1800,
    pull_status_path: Path | None = None,
) -> dict[str, Any]:
    """Project the local cursor and remote snapshot without network I/O.

    ``pull_status_path`` is separate from ``path`` because the two files are
    written by different owners (the importer writes the cursor, the launchd
    pull job writes its attempt status).  It is injectable so a caller passing
    a temporary cursor is not silently mixed with the real workstation's live
    pull state - which previously made this projection untestable in isolation
    and left its tests failing or passing according to whether the machine's
    last real pull happened to have succeeded.
    """
    cursor_path = path or _cursor_path()
    try:
        payload = json.loads(cursor_path.read_text(encoding="utf-8"))
        imported_at = parse_checkpoint(str(payload.get("imported_at") or ""), now=now)
        observed_at = now or datetime.now(timezone.utc)
        age_seconds = max(0.0, (observed_at - imported_at).total_seconds())
        runtime = payload.get("edge_runtime") if isinstance(payload.get("edge_runtime"), dict) else {}
        remote_ok = runtime.get("status") == "ok" and runtime.get("runtime_profile") == "intraday_edge"
        pull = read_pull_status(pull_status_path)
        sequence = parse_sequence(payload.get("sequence"))
        remote_sequence = parse_sequence(payload.get("remote_sequence"))
        sequence_lag = max(0, remote_sequence - sequence)
        catching_up = bool(payload.get("has_more")) or sequence_lag > 0
        remote_latest_changed_at = payload.get("remote_latest_changed_at")
        if remote_latest_changed_at:
            remote_latest_changed_at = parse_checkpoint(str(remote_latest_changed_at)).isoformat()
        state = "ready" if remote_ok and age_seconds <= stale_after_seconds else "stale" if runtime else "unavailable"
        if state == "ready" and catching_up:
            state = "catching_up"
        if state in {"ready", "catching_up"} and pull.get("state") == "failed":
            state = "degraded"
        return {
            "configured": True, "state": state, "last_imported_at": imported_at.isoformat(),
            "age_seconds": round(age_seconds, 1), "stale_after_seconds": stale_after_seconds,
            "checkpoint": payload.get("checkpoint"), "counts": payload.get("counts") or {},
            "sequence": sequence, "remote_sequence": remote_sequence,
            "sequence_lag": sequence_lag, "has_more": bool(payload.get("has_more")),
            "remote_latest_changed_at": remote_latest_changed_at,
            "runtime": runtime, "pull": pull,
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
    export_parser.add_argument("--after-sequence", default="")
    export_parser.add_argument("--limit", type=int, default=CHANGE_PAGE_SIZE)
    restricted_export_parser = subcommands.add_parser("restricted-export")
    restricted_export_parser.add_argument("original_command")
    pull_status_parser = subcommands.add_parser("pull-status")
    pull_status_parser.add_argument("--state", required=True, choices=("running", "catching_up", "completed", "failed"))
    pull_status_parser.add_argument("--error", default="")
    pull_status_parser.add_argument("--pages-imported", type=int)
    pull_status_parser.add_argument("--rows-imported", type=int)
    pull_status_parser.add_argument("--duration-ms", type=int)
    subcommands.add_parser("live-session-acceptance")
    subcommands.add_parser("import")
    cursor_parser = subcommands.add_parser("cursor")
    cursor_parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.command == "cursor":
        payload = read_cursor_payload()
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if arguments.json else payload["checkpoint"])
        return 0
    if arguments.command == "export":
        if str(arguments.after_sequence).strip():
            export_changes(parse_sequence(arguments.after_sequence), limit=arguments.limit)
            return 0
        export_jsonl(parse_since(arguments.since))
        return 0
    if arguments.command == "restricted-export":
        mode, value = parse_restricted_export_command(arguments.original_command)
        if mode == "export-changes":
            export_changes(int(value))
        else:
            export_jsonl(parse_since(str(value)))
        return 0
    if arguments.command == "pull-status":
        print(json.dumps(
            write_pull_status(
                arguments.state, error=arguments.error, pages_imported=arguments.pages_imported,
                rows_imported=arguments.rows_imported, duration_ms=arguments.duration_ms,
            ), ensure_ascii=False, separators=(",", ":"),
        ))
        return 0
    if arguments.command == "live-session-acceptance":
        result = run_live_session_acceptance()
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 1 if result.get("state") == "failed" else 0
    result = import_jsonl(sys.stdin)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "TRANSFER_TABLES", "TransferTable", "edge_evidence_status", "edge_runtime_snapshot",
    "CHANGE_PAGE_SIZE", "CHANGE_REPLAY_WINDOW", "export_changes", "export_jsonl", "import_jsonl",
    "assess_live_session_acceptance", "parse_checkpoint", "parse_restricted_export_command", "parse_sequence", "parse_since", "read_cursor", "read_cursor_payload", "read_live_session_acceptance", "read_pull_status", "run_live_session_acceptance", "upsert_statement", "write_live_session_acceptance", "write_pull_status",
]
