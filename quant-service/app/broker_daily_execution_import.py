"""Import a user-attributed THS 当日成交 export without inventing order status."""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
import shutil
from types import SimpleNamespace
from typing import Any

from .broker_export_parser import ParsedExport, parse_daily_execution_export
from .broker_trade_repository import persist_trade_batch


def _archive(source: Path, parsed: ParsedExport, day: date, root: Path) -> Path:
    folder = root.resolve() / str(day.year) / f"{day.month:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{parsed.sha256}{source.suffix.lower() or '.xls'}"
    if target.exists():
        if sha256(target.read_bytes()).hexdigest() != parsed.sha256:
            raise ValueError("BROKER_DAILY_EXECUTION_ARCHIVE_HASH_CONFLICT")
    else:
        shutil.copy2(source, target)
    return target


def import_daily_execution_export(
    connection: Any, source: Path, *, day: date, account_key: str, broker: str,
    archive_root: Path, confirmed_account_attribution: bool,
) -> dict[str, Any]:
    if not confirmed_account_attribution:
        raise ValueError("BROKER_DAILY_EXECUTION_ACCOUNT_ATTRIBUTION_REQUIRED")
    source = Path(source).resolve()
    parsed = parse_daily_execution_export(source, day)
    connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (account_key + ":broker-trade-import",))
    account_exists = connection.execute(
        "SELECT 1 FROM quant.broker_portfolio_snapshots WHERE account_key=%s LIMIT 1", (account_key,),
    ).fetchone()
    if not account_exists:
        raise ValueError("BROKER_DAILY_EXECUTION_ACCOUNT_KEY_UNKNOWN")
    binding = connection.execute(
        "SELECT 1 FROM quant.broker_order_account_bindings WHERE account_key=%s AND broker=%s LIMIT 1",
        (account_key, broker),
    ).fetchone()
    if not binding:
        raise ValueError("BROKER_DAILY_EXECUTION_BROKER_BINDING_MISSING")
    archived = _archive(source, parsed, day, Path(archive_root))
    snapshot = SimpleNamespace(
        account_key=account_key, observed_at=datetime.now(timezone.utc),
        metadata={"account_identity": {"broker": broker}},
    )
    batch = {"source": "ths_daily_execution_export", "source_sha256": parsed.sha256,
             "records": parsed.rows}
    symbols = sorted({row["symbol"] for row in parsed.rows})
    present = {row["symbol"] for row in connection.execute(
        "SELECT symbol FROM quant.instruments WHERE symbol=ANY(%s)", (symbols,),
    ).fetchall()}
    # An existing instrument needs no repeated ON CONFLICT UPDATE. Besides
    # unnecessary writes, that update can wait behind a long daily ingestion.
    persisted = persist_trade_batch(connection, batch, snapshot,
                                    upsert_instruments=present != set(symbols))
    return {
        "status": "imported" if persisted["inserted"] else "idempotent",
        "account_key": account_key, "trade_date": day.isoformat(),
        "source_sha256": parsed.sha256, "source_rows": len(parsed.rows) + len(parsed.ignored_rows),
        "fill_rows": len(parsed.rows), "zero_or_ignored_rows": len(parsed.ignored_rows),
        "ignored_reasons": {reason: sum(row["reason"] == reason for row in parsed.ignored_rows)
                            for reason in {row["reason"] for row in parsed.ignored_rows}},
        "inserted": persisted["inserted"], "idempotent": persisted["idempotent"],
        "verified_rows": persisted["verified_rows"], "archive_path": str(archived),
        "account_attribution": "user_explicit;source_has_no_account_or_date",
        "time_semantics": "exact_trade_time_for_positive_fills;zero_rows_not_proven_cancellations",
    }


def finalize_daily_execution_source(
    source: Path, parsed: ParsedExport, *, day: date, inbox_root: Path, processed_root: Path,
) -> dict[str, str]:
    source = Path(source).resolve()
    try:
        source.relative_to(Path(inbox_root).resolve())
    except ValueError as error:
        raise ValueError("BROKER_DAILY_EXECUTION_SOURCE_OUTSIDE_INBOX") from error
    if not source.is_file() or sha256(source.read_bytes()).hexdigest() != parsed.sha256:
        raise ValueError("BROKER_DAILY_EXECUTION_SOURCE_HASH_MISMATCH")
    folder = Path(processed_root).resolve() / str(day.year) / f"{day.month:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{day.isoformat()}_成交明细_{parsed.sha256[:8]}{source.suffix.lower() or '.xls'}"
    disposition = "moved"
    if target.exists():
        if sha256(target.read_bytes()).hexdigest() != parsed.sha256:
            raise ValueError("BROKER_DAILY_EXECUTION_PROCESSED_HASH_CONFLICT")
        source.unlink()
        disposition = "duplicate_source_removed"
    else:
        shutil.move(str(source), str(target))
    if sha256(target.read_bytes()).hexdigest() != parsed.sha256:
        raise ValueError("BROKER_DAILY_EXECUTION_PROCESSED_READBACK_MISMATCH")
    return {"source_disposition": disposition, "processed_path": str(target),
            "processed_sha256": parsed.sha256}


__all__ = ["import_daily_execution_export", "finalize_daily_execution_source"]
