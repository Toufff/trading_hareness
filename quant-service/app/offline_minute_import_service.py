"""Local-only minute CSV import and recovery boundary.

This module never opens a market-data connection.  It only reads a file under
the configured mounted directory, keeps the source availability clock honest,
and writes an idempotent import receipt plus bounded minute rows.
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from psycopg.types.json import Json


def data_root(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    root = Path(values.get("OFFLINE_DATA_DIR", "/var/lib/quant/offline")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def import_path(file_name: str, *, root: Path) -> Path:
    path = (root / file_name).resolve()
    if path.parent != root or not path.is_file():
        raise ValueError("offline CSV file does not exist in the configured offline directory")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def minute_timestamp(value: Any) -> datetime:
    """Parse vendor local timestamps; naive values mean Shanghai exchange time."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("minute row has no datetime")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("datetime must be ISO-8601 or YYYY-MM-DD HH:MM:SS") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.astimezone(timezone.utc)


def source_available_at(row: Mapping[str, Any]) -> datetime | None:
    """Only accept an explicit source clock; never derive it from bar time."""
    for key in ("source_available_at", "provider_available_at", "received_at", "available_at"):
        value = row.get(key)
        if value not in (None, ""):
            return minute_timestamp(value)
    return None


def minute_row(row: dict[str, Any], *, decimal_or_none: Callable[[Any], Any]) -> dict[str, Any]:
    symbol = str(row.get("ts_code") or row.get("symbol") or "").upper().strip()
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
        raise ValueError("minute row needs ts_code or symbol in 000001.SZ form")
    bar_time = minute_timestamp(row.get("datetime") or row.get("bar_time") or row.get("time"))
    open_price, high, low, close = (decimal_or_none(row.get(key)) for key in ("open", "high", "low", "close"))
    if any(value is None for value in (open_price, high, low, close)) or min(open_price, high, low, close) <= 0:
        raise ValueError("minute row needs positive open, high, low and close")
    if high < max(open_price, low, close) or low > min(open_price, high, close):
        raise ValueError("minute OHLC values are inconsistent")
    volume = decimal_or_none(row.get("volume") if row.get("volume") not in (None, "") else row.get("vol"))
    amount = decimal_or_none(row.get("amount"))
    if (volume is not None and volume < 0) or (amount is not None and amount < 0):
        raise ValueError("minute volume and amount must not be negative")
    return {
        "symbol": symbol, "bar_time": bar_time, "open": open_price, "high": high, "low": low,
        "close": close, "volume": volume, "amount": amount,
        "source_available_at": source_available_at(row), "raw": row,
    }


def stale_seconds(environ: Mapping[str, str] | None = None) -> int:
    values = os.environ if environ is None else environ
    try:
        return max(60, min(86_400, int(values.get("OFFLINE_MINUTE_IMPORT_STALE_SECONDS", "900"))))
    except ValueError:
        return 900


def recovery_action(existing: Mapping[str, Any] | None, *, now: datetime, stale_after_seconds: int) -> str:
    if existing is None:
        return "create"
    status = str(existing.get("status") or "")
    if status in {"completed", "partial"}:
        return "unchanged"
    if status == "failed":
        return "resume_failed"
    started_at = existing.get("started_at")
    if status == "running" and isinstance(started_at, datetime):
        started = started_at.replace(tzinfo=timezone.utc) if started_at.tzinfo is None else started_at.astimezone(timezone.utc)
        if now.astimezone(timezone.utc) - started < timedelta(seconds=stale_after_seconds):
            return "in_progress"
    return "resume_stale_running"


def ensure_instrument(connection: Any, symbol: str, *, exchange_for: Callable[[str], str]) -> None:
    connection.execute(
        "INSERT INTO quant.instruments(symbol,exchange,source) VALUES(%s,%s,'offline-import') ON CONFLICT(symbol) DO NOTHING",
        (symbol, exchange_for(symbol)),
    )


def import_csv(
    database: Any, request: Any, *, root: Path, exchange_for: Callable[[str], str],
    decimal_or_none: Callable[[Any], Any], safe_error: Callable[[str, int], str],
    stale_after_seconds: int, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Stream one mounted file with a durable hash-owned import receipt."""
    path = import_path(request.file_name, root=root)
    file_sha256 = sha256_file(path)
    started_at = now()
    with database.transaction() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (file_sha256,))
        existing = connection.execute(
            """SELECT import_id,source_name,file_name,status,row_count,rejected_rows,error_message,started_at
                 FROM quant.offline_imports WHERE file_sha256=%s FOR UPDATE""",
            (file_sha256,),
        ).fetchone()
        existing_row = dict(existing) if existing else None
        action = recovery_action(existing_row, now=started_at, stale_after_seconds=stale_after_seconds)
        if action == "unchanged":
            return {"status": "unchanged", "import_id": str(existing["import_id"]), "stored": existing["row_count"],
                    "rejected_rows": existing["rejected_rows"], "file_name": request.file_name}
        if action == "in_progress":
            return {"status": "running", "import_id": str(existing["import_id"]), "stored": existing["row_count"],
                    "rejected_rows": existing["rejected_rows"], "file_name": request.file_name,
                    "notice": "an active local import already owns this file hash; retry after its stale window"}
        if existing and str(existing["source_name"]) != request.source_name:
            raise ValueError("offline CSV hash already belongs to a different source_name")
        if existing:
            import_id = connection.execute(
                """UPDATE quant.offline_imports SET status='running',row_count=0,rejected_rows=0,error_message=NULL,
                       started_at=now(),finished_at=NULL WHERE import_id=%s RETURNING import_id""",
                (existing["import_id"],),
            ).fetchone()["import_id"]
        else:
            import_id = connection.execute(
                """INSERT INTO quant.offline_imports(source_name,file_name,file_sha256,dataset_kind,status)
                   VALUES(%s,%s,%s,'minute_bar','running') RETURNING import_id""",
                (request.source_name, request.file_name, file_sha256),
            ).fetchone()["import_id"]

    accepted = rejected = 0
    batch: list[dict[str, Any]] = []

    def write_batch(items: list[dict[str, Any]]) -> None:
        if not items:
            return
        with database.transaction() as connection:
            for item in items:
                ensure_instrument(connection, item["symbol"], exchange_for=exchange_for)
                connection.execute(
                    """INSERT INTO quant.market_bars_minute(symbol,bar_time,open,high,low,close,volume,amount,source_name,import_id,source_available_at,available_at,raw)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s)
                       ON CONFLICT(symbol,bar_time,source_name) DO UPDATE SET open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
                         close=EXCLUDED.close,volume=EXCLUDED.volume,amount=EXCLUDED.amount,import_id=EXCLUDED.import_id,
                         source_available_at=EXCLUDED.source_available_at,available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
                    (item["symbol"], item["bar_time"], item["open"], item["high"], item["low"], item["close"], item["volume"],
                     item["amount"], request.source_name, import_id, item["source_available_at"], Json(item["raw"])),
                )

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames:
                raise ValueError("offline CSV needs a header row")
            for line_number, row in enumerate(reader, start=2):
                if line_number - 1 > request.max_rows:
                    raise ValueError(f"offline CSV exceeds the {request.max_rows} row safety cap")
                try:
                    batch.append(minute_row(dict(row), decimal_or_none=decimal_or_none))
                    accepted += 1
                except (ValueError, ArithmeticError):
                    rejected += 1
                if len(batch) >= 1000:
                    write_batch(batch)
                    batch.clear()
            write_batch(batch)
        status = "completed" if rejected == 0 else "partial"
        with database.transaction() as connection:
            connection.execute(
                """UPDATE quant.offline_imports SET status=%s,row_count=%s,rejected_rows=%s,finished_at=now() WHERE import_id=%s""",
                (status, accepted, rejected, import_id),
            )
        return {"status": status, "import_id": str(import_id), "stored": accepted, "rejected_rows": rejected,
                "file_name": request.file_name, "file_sha256": file_sha256, "recovery_action": action}
    except Exception as error:
        with database.transaction() as connection:
            connection.execute(
                """UPDATE quant.offline_imports SET status='failed',row_count=%s,rejected_rows=%s,error_message=%s,
                   finished_at=now() WHERE import_id=%s""",
                (accepted, rejected, safe_error(str(error), 1000), import_id),
            )
        raise


__all__ = [
    "data_root", "ensure_instrument", "import_csv", "import_path", "minute_row", "minute_timestamp",
    "recovery_action", "sha256_file", "source_available_at", "stale_seconds",
]
