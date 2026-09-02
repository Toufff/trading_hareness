"""Resumable SQLite-to-PostgreSQL stock-brain archive migration."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
import hashlib
import sqlite3
import sys
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, uuid5

# Sibling modules in this relocated, non-package directory; make sure this
# file's own directory is importable regardless of how it was reached.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from legacy_stock_brain_contracts import (  # noqa: E402
    KNOWN_TABLES,
    coerce_timestamp,
    evidence_timestamps,
    exchange_for_symbol,
    normalize_symbol,
    normalized_payload,
    parse_json_value,
    payload_sha256,
    source_row_key,
    table_classification,
)
from legacy_stock_brain_repository import LegacyStockBrainRepository  # noqa: E402


DERIVED_SQLITE_TABLE_MARKERS = ("_fts", "_data", "_idx", "_content", "_docsize", "_config")
INSTRUMENT_SOURCE_TABLES = frozenset({
    "board_snapshots", "global_market_observations", "market_observations",
    "position_snapshots", "security_classifications", "security_order_flow_daily", "zt_snapshots",
})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_consistent_snapshot(source: Path, archive_root: Path) -> tuple[Path, str]:
    source = source.resolve(strict=True)
    archive_root.mkdir(parents=True, exist_ok=True)
    temporary = archive_root / f".{source.stem}-{datetime.now():%Y%m%d-%H%M%S}.partial.db"
    if temporary.exists():
        temporary.unlink()
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30)
    destination = sqlite3.connect(temporary)
    try:
        source_connection.backup(destination, pages=8192, sleep=0.02)
        result = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"stock-brain snapshot integrity check failed: {result}")
    finally:
        destination.close()
        source_connection.close()
    digest = sha256_file(temporary)
    final_path = archive_root / f"brain-{digest[:20]}.db"
    if final_path.exists():
        temporary.unlink()
    else:
        temporary.replace(final_path)
    return final_path, digest


def sqlite_tables(connection: sqlite3.Connection) -> list[str]:
    tables: list[str] = []
    for name, sql in connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        lowered = str(name).lower()
        if "virtual table" in str(sql or "").lower() or any(marker in lowered for marker in DERIVED_SQLITE_TABLE_MARKERS):
            continue
        tables.append(str(name))
    unknown = sorted(set(tables) - KNOWN_TABLES)
    if unknown:
        raise RuntimeError(f"stock-brain schema contains unclassified tables: {', '.join(unknown)}")
    return tables


def primary_key_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [row[1] for row in sorted(rows, key=lambda item: item[5]) if row[5] > 0]


def _instrument_rows(table: str, row: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates: list[tuple[Any, Any]] = []
    for code_field, name_field in (
        ("security_code", "security_name"), ("code", "name"), ("symbol", "name"),
        ("lead_code", "lead_stock"),
    ):
        if row.get(code_field):
            candidates.append((row.get(code_field), row.get(name_field)))
    result: list[dict[str, Any]] = []
    for raw_symbol, name in candidates:
        symbol = normalize_symbol(raw_symbol)
        if not symbol:
            continue
        result.append({
            "symbol": symbol,
            "exchange": exchange_for_symbol(symbol, venue=row.get("venue"), market=row.get("market")),
            "name": str(name or "").strip() or None,
            "source": f"stock-brain:{table}",
        })
    return result


def _money_flow(row: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any] | None:
    symbol = normalize_symbol(row.get("security_code"))
    available_at = coerce_timestamp(row.get("available_at") or row.get("received_at"))
    if not symbol or not row.get("trade_date") or available_at is None:
        return None
    source_key = str(row.get("source_key") or "unknown")
    convention = str(row.get("convention") or "vendor_main_net")
    return {
        "symbol": symbol,
        "trading_date": row["trade_date"],
        "source": f"legacy:{convention}",
        "provider": source_key.removeprefix("market:"),
        "net_amount": row.get("main_net"),
        "available_at": available_at,
        "raw": dict(payload),
    }


def _market_observation(table: str, row: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any] | None:
    if table == "market_observations":
        market = "CN"
        symbol = normalize_symbol(row.get("symbol"))
        effective_at = coerce_timestamp(row.get("observed_at"))
        available_at = coerce_timestamp(row.get("received_at") or row.get("observed_at"))
        close = row.get("price")
        settled = row.get("session_state") == "closed" and row.get("quality") == "settled"
        trading_date = row.get("trade_date")
        provider = f"legacy:{row.get('source') or 'unknown'}"
        is_final = settled
    else:
        market = str(row.get("market") or "GLOBAL")
        symbol = normalize_symbol(row.get("symbol"))
        effective_at = coerce_timestamp(row.get("event_time"))
        available_at = coerce_timestamp(row.get("available_at") or row.get("received_at") or row.get("event_time"))
        close = row.get("close")
        settled = bool(row.get("is_final")) and row.get("session_state") == "closed" and row.get("quality") == "settled"
        trading_date = row.get("local_trade_date")
        provider = f"legacy:{row.get('source') or 'unknown'}"
        is_final = bool(row.get("is_final"))
    if not symbol or not effective_at or not available_at or not trading_date or close is None:
        return None
    digest = str(row.get("payload_hash") or payload_sha256(payload))
    observation_id = uuid5(NAMESPACE_URL, f"stock-brain:{table}:{row.get('id')}:{digest}")
    normalized = {
        "trading_date": trading_date, "open": row.get("open"), "high": row.get("high"),
        "low": row.get("low"), "close": close, "pre_close": row.get("prev_close"),
        "volume": row.get("volume"), "amount": row.get("amount"), "is_final": is_final,
        "legacy_quality": row.get("quality"), "legacy_session_state": row.get("session_state"),
    }
    return {
        "observation_id": observation_id, "provider": provider, "market": market,
        "symbol": symbol, "effective_at": effective_at, "available_at": available_at,
        "ingested_at": coerce_timestamp(row.get("received_at")), "payload_sha256": digest,
        "normalized": normalized, "payload": dict(payload), "settled": settled,
        "trading_date": trading_date, "open": row.get("open"), "high": row.get("high"),
        "low": row.get("low"), "close": close, "pre_close": row.get("prev_close"),
        "volume": row.get("volume"), "amount": row.get("amount"),
    }


def _as_list(value: Any) -> list[Any]:
    parsed = parse_json_value(value, None)
    if parsed is None:
        return [] if value in (None, "") else [str(value)]
    return parsed if isinstance(parsed, list) else [parsed]


def _as_object(value: Any) -> dict[str, Any]:
    parsed = parse_json_value(value, {})
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _journal_entry(row: Mapping[str, Any], digest: str) -> dict[str, Any] | None:
    if not row.get("date"):
        return None
    return {
        "entry_date": row["date"], "title": f"stock-brain 每日复盘 {row['date']}",
        "body": str(row.get("reflection") or ""), "positions": _as_list(row.get("positions")),
        "actions": _as_list(row.get("action")), "plans": _as_list(row.get("plan")),
        "watchlist": _as_list(row.get("watchlist")), "discipline": _as_object(row.get("discipline")),
        "source_record_key": str(row.get("id")), "content_hash": digest,
        "metadata": {"legacy_created_at": row.get("created_at")},
        "created_at": coerce_timestamp(row.get("created_at")) or coerce_timestamp(row.get("date")),
    }


def _partial_position_history(source: sqlite3.Connection) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, Any], dict[str, Any]] = defaultdict(lambda: {"positions": {}, "sources": set()})
    for source_row in source.execute("SELECT * FROM position_snapshots ORDER BY as_of,id"):
        row = dict(source_row)
        observed_at = coerce_timestamp(row.get("as_of"))
        symbol = normalize_symbol(row.get("security_code"))
        if observed_at is None or not symbol or row.get("quantity") is None:
            continue
        key = (row.get("portfolio_id"), observed_at)
        grouped[key]["sources"].add(str(row.get("source") or "unknown"))
        weight = row.get("weight")
        try:
            weight_pct = None if weight is None else float(weight) * (100 if abs(float(weight)) <= 1 else 1)
        except (TypeError, ValueError):
            weight_pct = None
        grouped[key]["positions"][symbol] = {
            "symbol": symbol, "name": str(row.get("security_name") or symbol),
            "quantity": row.get("quantity"),
            "sellable_quantity": row.get("available_quantity") if row.get("available_quantity") is not None else row.get("quantity"),
            "average_cost": row.get("average_cost"), "position_weight_pct": weight_pct,
            "metadata": {
                "legacy_entity_id": row.get("entity_id"), "legacy_row_id": row.get("id"),
                "benchmark_code": row.get("benchmark_code"),
                "migration_policy": "broker facts only; plan_json and triggers_json discarded",
            },
        }
    results: list[dict[str, Any]] = []
    for (portfolio_id, observed_at), group in grouped.items():
        positions = [group["positions"][symbol] for symbol in sorted(group["positions"])]
        content = {
            "portfolio_id": portfolio_id, "observed_at": observed_at.isoformat(),
            "positions": positions, "sources": sorted(group["sources"]),
        }
        digest = payload_sha256(content)
        results.append({
            "source_snapshot_key": f"stock-brain-history:{observed_at.isoformat()}:{digest[:16]}",
            "observed_at": observed_at, "content_hash": digest, "positions": positions,
            "metadata": {
                "legacy_portfolio_id": portfolio_id, "legacy_sources": sorted(group["sources"]),
                "verification_note": "historic position rows lack account totals and screenshot reconciliation",
            },
        })
    return results


class LegacyStockBrainArchiveImporter:
    def __init__(
        self,
        repository: LegacyStockBrainRepository,
        *,
        batch_size: int = 1000,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.repository = repository
        self.batch_size = max(100, min(int(batch_size), 5000))
        self.progress = progress or (lambda _message: None)

    def run(self, snapshot: Path, digest: str) -> dict[str, Any]:
        snapshot_key = f"sha256:{digest}"
        import_run_id, existing_status = self.repository.begin_import(
            source_snapshot_key=snapshot_key,
            source_path=str(snapshot),
            source_sha256=digest,
            source_size_bytes=snapshot.stat().st_size,
        )
        if existing_status == "completed":
            return {"status": "already_completed", "import_run_id": str(import_run_id), "snapshot": str(snapshot)}

        connection = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        summary = {"tables_completed": 0, "tables_excluded": 0, "archived_rows": 0, "canonical_rows": 0}
        try:
            for table in sqlite_tables(connection):
                source_count = int(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
                classification = table_classification(table)
                self.progress(f"{table}: {classification or 'excluded'} ({source_count} rows)")
                if classification is None:
                    self.repository.exclude_table(import_run_id, table, source_count)
                    summary["tables_excluded"] += 1
                    continue
                self._import_table(connection, import_run_id, table, classification, source_count, summary)
                summary["tables_completed"] += 1
            self.repository.finish_import(import_run_id, summary)
            return {"status": "completed", "import_run_id": str(import_run_id), "snapshot": str(snapshot), **summary}
        except Exception as error:
            self.repository.fail_import(import_run_id, f"{type(error).__name__}: {error}")
            raise
        finally:
            connection.close()

    def _import_table(
        self,
        source: sqlite3.Connection,
        import_run_id,
        table: str,
        classification: str,
        source_count: int,
        summary: dict[str, int],
    ) -> None:
        self.repository.start_table(
            import_run_id=import_run_id, table=table, classification=classification, source_count=source_count,
        )
        receipt = self.repository.receipt(import_run_id, table) or {}
        if receipt.get("status") == "completed":
            summary["archived_rows"] += int(receipt.get("archived", 0))
            summary["canonical_rows"] += int(receipt.get("canonical", 0))
            return
        last_rowid = int(receipt.get("last_rowid", 0))
        archived = int(receipt.get("archived", 0))
        canonical = int(receipt.get("canonical", 0))
        skipped = int(receipt.get("skipped", 0))
        keys = primary_key_columns(source, table)

        while True:
            rows = source.execute(
                f'SELECT rowid AS __rowid__,* FROM "{table}" WHERE rowid>? ORDER BY rowid LIMIT ?',
                (last_rowid, self.batch_size),
            ).fetchall()
            if not rows:
                if table == "position_snapshots":
                    partial_snapshots = _partial_position_history(source)
                    instrument_rows = [
                        {
                            "symbol": position["symbol"],
                            "exchange": exchange_for_symbol(position["symbol"]),
                            "name": position.get("name"),
                            "source": "stock-brain:position_snapshots",
                        }
                        for snapshot in partial_snapshots for position in snapshot["positions"]
                    ]
                    with self.repository.transaction() as cursor:
                        self.repository.upsert_instruments(cursor, instrument_rows)
                        self.repository.upsert_partial_broker_snapshots(cursor, partial_snapshots)
                    canonical = max(canonical, len(partial_snapshots))
                with self.repository.transaction() as cursor:
                    self.repository.update_receipt(
                        cursor, import_run_id=import_run_id, table=table, last_rowid=last_rowid,
                        archived=archived, canonical=canonical, skipped=skipped, completed=True,
                    )
                summary["archived_rows"] += archived
                summary["canonical_rows"] += canonical
                return

            records: list[dict[str, Any]] = []
            instruments: list[dict[str, Any]] = []
            money_flows: list[dict[str, Any]] = []
            market_rows: list[dict[str, Any]] = []
            journals: list[dict[str, Any]] = []
            for source_row in rows:
                row = dict(source_row)
                payload = normalized_payload(row)
                row_digest = payload_sha256(payload)
                effective_at, available_at = evidence_timestamps(row)
                records.append({
                    "table": table, "row_key": source_row_key(row, keys),
                    "classification": classification, "effective_at": effective_at,
                    "available_at": available_at, "payload_sha256": row_digest, "payload": payload,
                })
                if table in INSTRUMENT_SOURCE_TABLES:
                    instruments.extend(_instrument_rows(table, row))
                if table == "security_order_flow_daily":
                    flow = _money_flow(row, payload)
                    if flow:
                        money_flows.append(flow)
                elif table in {"market_observations", "global_market_observations"}:
                    observation = _market_observation(table, row, payload)
                    if observation:
                        market_rows.append(observation)
                elif table == "personal_reviews":
                    journal = _journal_entry(row, row_digest)
                    if journal:
                        journals.append(journal)

            batch_canonical = len(money_flows) + len(market_rows) + len(journals)
            next_archived = archived + len(records)
            next_canonical = canonical + batch_canonical
            next_last_rowid = int(rows[-1]["__rowid__"])
            with self.repository.transaction() as cursor:
                self.repository.archive_rows(cursor, import_run_id, records)
                self.repository.upsert_instruments(cursor, instruments)
                self.repository.upsert_money_flows(cursor, money_flows)
                self.repository.upsert_market_observations(cursor, market_rows)
                self.repository.upsert_journal_entries(cursor, journals)
                self.repository.update_receipt(
                    cursor, import_run_id=import_run_id, table=table, last_rowid=next_last_rowid,
                    archived=next_archived, canonical=next_canonical, skipped=skipped,
                )
            archived, canonical, last_rowid = next_archived, next_canonical, next_last_rowid


__all__ = [
    "LegacyStockBrainArchiveImporter", "create_consistent_snapshot", "sha256_file", "sqlite_tables",
]
