"""Immutable persistence for user-authorized broker trade exports."""
from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re

from psycopg.types.json import Json

from .broker_export_parser import parse_trade_export
from .instrument_registry import ensure_named_instruments


def _canonical_trade(row):
    value = dict(row)
    metadata = dict(value.get("metadata") or {})
    metadata.pop("source_line", None)
    value["metadata"] = metadata
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def load_trade_batch(path, run, snapshot):
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > 50_000_000:
        raise ValueError("BROKER_TRADE_BATCH_INVALID")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if value.get("schema_version") != "broker-trade-export-v1" or value.get("source") != "ths_desktop_export":
        raise ValueError("BROKER_TRADE_BATCH_INVALID")
    if str(value.get("run_id")) != str(run["run_id"]) or value.get("account_key") != snapshot.account_key:
        raise ValueError("BROKER_TRADE_BATCH_RUN_MISMATCH")
    if value.get("source_sha256") not in {item.get("sha256") for item in snapshot.metadata.get("evidence", [])
                                           if "trades" in item.get("roles", [])}:
        raise ValueError("BROKER_TRADE_EXPORT_EVIDENCE_MISMATCH")
    records = value.get("records")
    if not isinstance(records, list):
        raise ValueError("BROKER_TRADE_RECORDS_MISSING")
    evidence = [item for item in snapshot.metadata.get("evidence", []) if "trades" in item.get("roles", [])]
    if len(evidence) != 1:
        raise ValueError("BROKER_TRADE_EXPORT_EVIDENCE_MISMATCH")
    reparsed = parse_trade_export(Path(evidence[0]["path"]))
    canonical = lambda rows: json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    if canonical(records) != canonical(reparsed.rows):
        raise ValueError("BROKER_TRADE_EXPORT_ROWS_MISMATCH")
    keys = set()
    for row in records:
        if not re.fullmatch(r"[0-9a-f]{64}", str(row.get("trade_key", ""))) or row["trade_key"] in keys:
            raise ValueError("BROKER_TRADE_KEY_INVALID")
        keys.add(row["trade_key"])
        if row.get("side") not in {"buy", "sell"} or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(row.get("symbol", ""))):
            raise ValueError("BROKER_TRADE_RECORD_INVALID")
        if Decimal(str(row.get("quantity"))) <= 0:
            raise ValueError("BROKER_TRADE_RECORD_INVALID")
    return value


def persist_trade_batch(connection, batch, snapshot, *, upsert_instruments=True):
    source_hash = batch["source_sha256"]
    broker = snapshot.metadata["account_identity"]["broker"]
    inserted = idempotent = 0
    # One ascending statement per batch, ahead of the record loop, instead of
    # one ``ON CONFLICT DO UPDATE`` per record in export order.  Hoisting it
    # also means a batch that later raises ``BROKER_TRADE_IMMUTABLE_CONFLICT``
    # has already registered its instruments -- harmless, because the caller
    # owns the transaction and that error rolls the whole batch back.
    if upsert_instruments:
        ensure_named_instruments(
            connection,
            [(row["symbol"], row["name"]) for row in batch["records"]],
            "ths_desktop_export",
        )
    for row in batch["records"]:
        existing = connection.execute(
            "SELECT source_sha256,metadata FROM quant.broker_trade_records WHERE account_key=%s AND trade_key=%s",
            (snapshot.account_key, row["trade_key"]),
        ).fetchone()
        expected = _canonical_trade(row)
        if existing:
            stored = (existing.get("metadata") or {}).get("canonical_row")
            if stored != expected:
                raise ValueError("BROKER_TRADE_IMMUTABLE_CONFLICT")
            idempotent += 1
            continue
        connection.execute(
            """INSERT INTO quant.broker_trade_records(
                   account_key,trade_key,broker,trade_date,trade_time,symbol,name,side,quantity,price,
                   gross_amount,net_amount,commission,stamp_duty,transfer_fee,other_fee,currency,
                   source,source_sha256,observed_at,metadata)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (snapshot.account_key, row["trade_key"], broker, row["trade_date"], row.get("trade_time"),
             row["symbol"], row["name"], row["side"], row["quantity"], row.get("price"),
             row.get("gross_amount"), row.get("net_amount"), row.get("commission", 0),
             row.get("stamp_duty", 0), row.get("transfer_fee", 0), row.get("other_fee", 0),
             row.get("currency") or "人民币", batch["source"], source_hash, snapshot.observed_at,
             Json({"canonical_row": expected, **(row.get("metadata") or {})})),
        )
        inserted += 1
    count = connection.execute(
        "SELECT count(*) AS n FROM quant.broker_trade_records WHERE account_key=%s AND trade_key=ANY(%s)",
        (snapshot.account_key, list(row["trade_key"] for row in batch["records"])),
    ).fetchone()["n"] if batch["records"] else 0
    if count != len(batch["records"]):
        raise ValueError("BROKER_TRADE_DATABASE_READBACK_MISMATCH")
    return {"inserted": inserted, "idempotent": idempotent, "verified_rows": count,
            "batch_hash": sha256(json.dumps(batch["records"], ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":"), default=str).encode()).hexdigest()}


__all__ = ["load_trade_batch", "persist_trade_batch"]
