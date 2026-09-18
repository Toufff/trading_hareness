"""Persistence and read projections for manually exported broker order history."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import shutil
from typing import Any

from psycopg.types.json import Json

from .broker_order_export_parser import ParsedOrderExport, parse_order_export
from .broker_order_timeline import map_execution_to_bars
from .instrument_registry import ensure_named_instruments


def _archive(path: Path, parsed: ParsedOrderExport, archive_root: Path) -> Path:
    suffix = path.suffix.lower() if path.suffix else ".xls"
    folder = Path(archive_root) / str(parsed.max_order_date.year) / f"{parsed.max_order_date.month:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{parsed.sha256}{suffix}"
    if target.exists():
        if sha256(target.read_bytes()).hexdigest() != parsed.sha256:
            raise ValueError("BROKER_ORDER_ARCHIVE_HASH_CONFLICT")
    else:
        shutil.copy2(path, target)
    return target.resolve()


def _json(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def import_order_export(
    connection: Any, path: Path, *, account_key: str, broker: str,
    archive_root: Path, confirm_account_binding: bool = False,
    confirmed_at: datetime | None = None,
) -> dict[str, Any]:
    if not account_key or not broker:
        raise ValueError("BROKER_ORDER_ACCOUNT_BINDING_REQUIRED")
    parsed = parse_order_export(Path(path).resolve())
    archived = _archive(Path(path).resolve(), parsed, Path(archive_root))
    confirmed_at = confirmed_at or datetime.now(timezone.utc)
    # The caller owns the transaction so the complete import is atomic.
    connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (account_key + ":broker-order-import",))
    account_exists = connection.execute(
            "SELECT 1 FROM quant.broker_portfolio_snapshots WHERE account_key=%s LIMIT 1", (account_key,),
    ).fetchone()
    if not account_exists:
        raise ValueError("BROKER_ORDER_ACCOUNT_KEY_UNKNOWN")
    binding = connection.execute(
            "SELECT account_key,broker,masked_account FROM quant.broker_order_account_bindings WHERE account_fingerprint=%s",
            (parsed.account_fingerprint,),
    ).fetchone()
    if binding and (binding["account_key"] != account_key or binding["broker"] != broker):
        raise ValueError("BROKER_ORDER_ACCOUNT_BINDING_MISMATCH")
    if not binding:
        if not confirm_account_binding:
            raise ValueError("BROKER_ORDER_ACCOUNT_BINDING_CONFIRMATION_REQUIRED")
        connection.execute(
                """INSERT INTO quant.broker_order_account_bindings(
                       account_fingerprint,account_key,broker,masked_account,confirmed_at,metadata)
                   VALUES(%s,%s,%s,%s,%s,%s)""",
                (parsed.account_fingerprint, account_key, broker, parsed.masked_account, confirmed_at,
                 Json({"basis": "explicit_user_requested_import", "source_sha256": parsed.sha256})),
        )
    existing = connection.execute(
            """SELECT import_id,row_count,execution_count,min_order_date,max_order_date,source_path
                 FROM quant.broker_order_imports WHERE account_key=%s AND source_sha256=%s""",
            (account_key, parsed.sha256),
    ).fetchone()
    if existing:
        return {
                "status": "idempotent", "import_id": str(existing["import_id"]), "account_key": account_key,
                "source_sha256": parsed.sha256, "row_count": existing["row_count"],
                "execution_count": existing["execution_count"], "inserted_events": 0,
                "min_order_date": str(existing["min_order_date"]), "max_order_date": str(existing["max_order_date"]),
                "archive_path": existing["source_path"],
        }
    imported = connection.execute(
            """INSERT INTO quant.broker_order_imports(
                   account_key,source_sha256,source_path,source_format,encoding,row_count,execution_count,
                   ignored_count,min_order_date,max_order_date,metadata)
               VALUES(%s,%s,%s,'ths_order_query_text_v1',%s,%s,%s,%s,%s,%s,%s)
               RETURNING import_id""",
            (account_key, parsed.sha256, str(archived), parsed.encoding, len(parsed.events), len(parsed.executions),
             len(parsed.ignored_rows), parsed.min_order_date, parsed.max_order_date,
             Json({"header_row": parsed.header_row, "masked_account": parsed.masked_account,
                   "strict_execution_rule": "status in full_or_partial_fill and quantity_price_amount_positive"})),
    ).fetchone()
    import_id = imported["import_id"]
    execution_keys = {row["source_event_key"]: row["execution_key"] for row in parsed.executions}
    inserted = idempotent = 0
    # One ascending statement for the whole export, ahead of the event loop,
    # in place of one ``ON CONFLICT DO UPDATE`` per event in export order.  A
    # single .xls covers many symbols and the events arrive in the order the
    # broker printed them, so the per-row form took the strongest lock class
    # on ``quant.instruments`` in an order no other writer shares.  The rows
    # are registered before any ``broker_order_events`` row references them,
    # which is the only ordering this function needs.
    ensure_named_instruments(
        connection,
        [(event["symbol"], event["name"]) for event in parsed.events if event["symbol"]],
        "ths_order_query_export",
    )
    for event in parsed.events:
        result = connection.execute(
                """INSERT INTO quant.broker_order_events(
                       account_key,event_key,first_import_id,source_sha256,order_date,order_at,symbol,raw_symbol,name,
                       side,raw_side,status,business_type,order_number,market,order_kind,cancel_flag,order_quantity,
                       filled_quantity,gross_amount,order_price,fill_price,cancel_requested_quantity,cancelled_quantity,
                       is_execution,time_basis,metadata)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                          'order_time_proxy',%s)
                   ON CONFLICT(account_key,event_key) DO NOTHING
                   RETURNING event_id""",
                (account_key, event["event_key"], import_id, parsed.sha256, event["order_date"], event["order_at"],
                 event["symbol"], event["raw_symbol"], event["name"], event["side"], event["raw_side"],
                 event["status"], event["business_type"], event["order_number"], event["market"], event["order_kind"],
                 event["cancel_flag"], event["order_quantity"], event["filled_quantity"], event["gross_amount"],
                 event["order_price"], event["fill_price"], event["cancel_requested_quantity"],
                 event["cancelled_quantity"], event["event_key"] in execution_keys,
                 Json({**event["metadata"], "execution_key": execution_keys.get(event["event_key"])})),
        ).fetchone()
        if result:
            inserted += 1
        else:
            idempotent += 1
    verified = connection.execute(
            "SELECT count(*) AS n FROM quant.broker_order_events WHERE account_key=%s AND event_key=ANY(%s)",
            (account_key, [row["event_key"] for row in parsed.events]),
    ).fetchone()["n"]
    if verified != len(parsed.events):
        raise ValueError("BROKER_ORDER_DATABASE_READBACK_MISMATCH")
    return {
        "status": "imported", "import_id": str(import_id), "account_key": account_key,
        "source_sha256": parsed.sha256, "row_count": len(parsed.events), "execution_count": len(parsed.executions),
        "ignored_count": len(parsed.ignored_rows), "inserted_events": inserted, "idempotent_events": idempotent,
        "verified_events": verified, "min_order_date": parsed.min_order_date.isoformat(),
        "max_order_date": parsed.max_order_date.isoformat(), "archive_path": str(archived),
        "time_semantics": "order_time_proxy_until_exact_trade_export_is_available",
    }


async def order_history_summary(async_database: Any, account_key: str) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT count(*)::int AS imports,max(imported_at) AS latest_import_at,
                      min(min_order_date) AS min_order_date,max(max_order_date) AS max_order_date,
                      coalesce(sum(row_count),0)::int AS imported_rows,
                      coalesce(sum(execution_count),0)::int AS imported_execution_rows
                 FROM quant.broker_order_imports WHERE account_key=%s""", (account_key,),
        )
        imports = dict(await result.fetchone())
        event_result = await connection.execute(
            """SELECT count(*)::int AS unique_events,
                      count(*) FILTER (WHERE is_execution)::int AS executions,
                      count(DISTINCT symbol) FILTER (WHERE is_execution)::int AS traded_symbols,
                      count(DISTINCT order_date) FILTER (WHERE is_execution)::int AS active_trade_dates
                 FROM quant.broker_order_events WHERE account_key=%s""", (account_key,),
        )
        events = dict(await event_result.fetchone())
    return {
        "account_key": account_key, **{key: _json(value) for key, value in imports.items()}, **events,
        "time_semantics": "委托时间不是成交时间；分钟映射使用成交价首次触达推断并标记置信度",
    }


async def order_history_timeline(
    async_database: Any, *, account_key: str, symbol: str, start_date: date, end_date: date, limit: int = 1500,
) -> dict[str, Any]:
    if end_date < start_date or (end_date - start_date).days > 31:
        raise ValueError("broker order timeline must be ordered and no longer than 31 days")
    bounded = max(60, min(int(limit), 3000))
    async with async_database.transaction() as connection:
        event_result = await connection.execute(
            """SELECT event_key,order_date,order_at,symbol,name,side,raw_side,status,order_number,
                      order_quantity,filled_quantity,gross_amount,order_price,fill_price,is_execution,time_basis
                 FROM quant.broker_order_events
                WHERE account_key=%s AND symbol=%s AND order_date BETWEEN %s AND %s
                ORDER BY order_at,event_key LIMIT %s""", (account_key, symbol, start_date, end_date, bounded),
        )
        events = [dict(row) for row in await event_result.fetchall()]
        bar_end = end_date + timedelta(days=7)
        bar_result = await connection.execute(
            """SELECT bar_time,open,high,low,close,volume,amount,source_name,available_at
                 FROM quant.intraday_minute_sessions
                WHERE symbol=%s AND trading_date BETWEEN %s AND %s
                ORDER BY bar_time LIMIT %s""", (symbol, start_date, bar_end, bounded),
        )
        bars = [dict(row) for row in await bar_result.fetchall()]
        bar_source = "intraday_minute_sessions"
        if not bars:
            fallback = await connection.execute(
                """SELECT bar_time,open,high,low,close,volume,amount,source_name,available_at
                     FROM quant.market_bars_minute
                    WHERE symbol=%s AND (bar_time AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                    ORDER BY bar_time LIMIT %s""", (symbol, start_date, bar_end, bounded),
            )
            bars = [dict(row) for row in await fallback.fetchall()]
            bar_source = "market_bars_minute"
    markers = []
    for event in events:
        marker = {key: _json(value) for key, value in event.items()}
        if event["is_execution"]:
            marker.update(map_execution_to_bars({"order_at": event["order_at"], "price": event["fill_price"]}, bars))
        else:
            marker.update({"mapping_status": "not_executed", "confidence": "none"})
        markers.append(marker)
    return {
        "account_key": account_key, "symbol": symbol, "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
        "bar_source": bar_source, "bar_count": len(bars), "event_count": len(events),
        "execution_count": sum(bool(row["is_execution"]) for row in events),
        "bars": [{key: _json(value) for key, value in row.items()} for row in bars], "events": markers,
        "boundary": "成交点是委托后成交价首次触达推断；精确成交时间需成交明细覆盖",
    }


__all__ = ["import_order_export", "order_history_summary", "order_history_timeline"]
