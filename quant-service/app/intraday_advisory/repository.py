"""Durable signal, delivery, model-run and runtime status storage."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from typing import Any

from psycopg.types.json import Json

from .rules import AdvisorySignal


def persist_quote_samples(connection: Any, observed_at: datetime, rows: list[dict[str, Any]]) -> int:
    stored = 0
    for row in rows:
        symbol = str(row.get("ts_code") or "")
        if not symbol:
            continue
        raw = dict(row)
        price = float(row.get("price") or 0)
        pre_close = float(row.get("pre_close") or 0)
        pct = (price / pre_close - 1) * 100 if price > 0 and pre_close > 0 else None
        result = connection.execute("""
            INSERT INTO quant.intraday_quote_observations(
                scan_id,symbol,observed_at,source_name,price,pct_change,raw)
            VALUES(NULL,%s,%s,'longhu_order_book',%s,%s,%s)
            ON CONFLICT(symbol,source_name,observed_at) DO NOTHING""",
            (symbol, observed_at, price, pct, Json(raw)))
        stored += int(result.rowcount or 0)
    return stored


def persist_index_samples(connection: Any, observed_at: datetime, rows: list[dict[str, Any]]) -> int:
    stored = 0
    for row in rows:
        symbol = str(row.get("ts_code") or "")
        try:
            price, pre_close = float(row.get("price") or 0), float(row.get("pre_close") or 0)
        except (TypeError, ValueError):
            continue
        if not symbol or price <= 0 or pre_close <= 0:
            continue
        result = connection.execute("""
            INSERT INTO quant.intraday_quote_observations(
                scan_id,symbol,observed_at,source_name,price,pct_change,raw)
            VALUES(NULL,%s,%s,'longhu_index_minute',%s,%s,%s)
            ON CONFLICT(symbol,source_name,observed_at) DO NOTHING""",
            (symbol, observed_at, price, (price / pre_close - 1) * 100, Json(dict(row))))
        stored += int(result.rowcount or 0)
    return stored


def latest_sector_snapshot(connection: Any, *, at: datetime) -> dict[str, Any] | None:
    row = connection.execute("""
        SELECT snapshot_minute,observed_at,payload->'items' AS items
          FROM quant.intraday_board_flow_snapshots
         WHERE snapshot_minute>=date_trunc('day',%s AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'
           AND snapshot_minute<=%s AND status IN ('completed','partial')
         ORDER BY snapshot_minute DESC LIMIT 1""", (at, at)).fetchone()
    return dict(row) if row else None


def persist_signal(connection: Any, signal: AdvisorySignal, *, scope_source: str) -> dict[str, Any] | None:
    row = connection.execute("""
        INSERT INTO quant.intraday_advisory_events(
            event_key,symbol,name,event_kind,direction,severity,observed_at,scope_source,metrics,summary)
        SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s
         WHERE NOT EXISTS (
             SELECT 1 FROM quant.intraday_advisory_events
              WHERE symbol=%s AND event_kind=%s AND direction=%s
                AND observed_at>%s-make_interval(secs=>600)
                AND (severity='high' OR %s<>'high'))
        ON CONFLICT(event_key) DO NOTHING RETURNING event_id,event_key""",
        (signal.event_key, signal.symbol, signal.name, signal.kind, signal.direction, signal.severity,
         signal.observed_at, scope_source, Json(signal.metrics), signal.summary,
         signal.symbol, signal.kind, signal.direction, signal.observed_at, signal.severity)).fetchone()
    return dict(row) if row else None


def enqueue_delivery(connection: Any, *, key: str, kind: str, text: str,
                     card: dict[str, Any], event_id: Any = None,
                     analysis_run_id: Any = None) -> dict[str, Any] | None:
    row = connection.execute("""
        INSERT INTO quant.intraday_advisory_deliveries(
            idempotency_key,delivery_kind,event_id,analysis_run_id,status,message_text,message_card,next_attempt_at)
        VALUES(%s,%s,%s,%s,'pending',%s,%s,now())
        ON CONFLICT(idempotency_key) DO NOTHING RETURNING delivery_id""",
        (key, kind, event_id, analysis_run_id, text, Json(card))).fetchone()
    return dict(row) if row else None


def due_deliveries(connection: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute("""
        SELECT delivery_id,idempotency_key,message_text,message_card,attempt_count,delivery_kind
          FROM quant.intraday_advisory_deliveries
         WHERE status IN ('pending','failed') AND attempt_count<8
           AND coalesce(next_attempt_at,created_at)<=now()
         ORDER BY CASE delivery_kind WHEN 'signal' THEN 0 ELSE 1 END,created_at LIMIT %s""",
        (max(1, min(limit, 50)),)).fetchall()]


def persist_delivery_outcome(connection: Any, delivery_id: Any, outcome: dict[str, Any]) -> None:
    status = str(outcome.get("status") or "failed")
    if status not in {"sent", "failed", "disabled"}:
        status = "failed"
    connection.execute("""
        UPDATE quant.intraday_advisory_deliveries
           SET status=%s,response=%s,error_message=%s,attempt_count=attempt_count+1,
               sent_at=CASE WHEN %s='sent' THEN now() ELSE sent_at END,
               next_attempt_at=CASE WHEN %s='failed' AND attempt_count+1<8
                    THEN now()+make_interval(secs=>least(900,30*power(2,least(attempt_count,5)))::double precision)
                    ELSE NULL END,updated_at=now()
         WHERE delivery_id=%s""",
        (status, Json(outcome.get("response") if isinstance(outcome.get("response"), dict) else {}),
         str(outcome.get("error") or outcome.get("reason") or "")[:500] or None,
         status, status, delivery_id))


def persist_analysis(connection: Any, *, provider: str, trigger_kind: str, started_at: datetime,
                     completed_at: datetime, input_payload: dict[str, Any], output: dict[str, Any] | None,
                     status: str, error: str | None = None, report_kind: str | None = None) -> dict[str, Any]:
    input_hash = sha256(json.dumps(input_payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    output_hash = sha256(json.dumps(output or {}, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest() if output else None
    row = connection.execute("""
        INSERT INTO quant.intraday_advisory_analysis_runs(
            provider,trigger_kind,report_kind,started_at,completed_at,status,input_hash,input_payload,
            output_hash,output,error_message)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING analysis_run_id""",
        (provider, trigger_kind, report_kind, started_at, completed_at, status, input_hash,
         Json(input_payload), output_hash, Json(output or {}), str(error or "")[:500] or None)).fetchone()
    return {"analysis_run_id": str(row["analysis_run_id"]), "output_hash": output_hash}


def latest_delivered_deepseek_fingerprint(connection: Any) -> str | None:
    row = connection.execute("""
        SELECT r.output->>'state_fingerprint' AS fingerprint
          FROM quant.intraday_advisory_analysis_runs r
          JOIN quant.intraday_advisory_deliveries d ON d.analysis_run_id=r.analysis_run_id
         WHERE r.provider='deepseek' AND r.status='completed' AND d.status='sent'
         ORDER BY r.completed_at DESC LIMIT 1""").fetchone()
    return str(row["fingerprint"]) if row and row.get("fingerprint") else None


def recent_discipline_events(connection: Any, *, after: datetime, limit: int = 20) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute("""
        SELECT event_id,observed_at,payload,line_kind,to_state
          FROM quant.discipline_alert_events WHERE observed_at>%s
         ORDER BY observed_at,event_id LIMIT %s""", (after, max(1, min(limit, 50)))).fetchall()]


def update_status(connection: Any, *, state: str, account_key: str, now: datetime,
                  scope_size: int, details: dict[str, Any], error: str | None = None) -> None:
    connection.execute("""
        INSERT INTO quant.intraday_advisory_runtime_status(
            runtime_key,state,account_key,last_tick_at,scope_size,last_error,details)
        VALUES('primary',%s,%s,%s,%s,%s,%s)
        ON CONFLICT(runtime_key) DO UPDATE SET state=excluded.state,account_key=excluded.account_key,
            last_tick_at=excluded.last_tick_at,scope_size=excluded.scope_size,last_error=excluded.last_error,
            details=excluded.details,updated_at=now()""",
        (state, account_key, now, scope_size, str(error or "")[:500] or None, Json(details)))


__all__ = ["due_deliveries", "enqueue_delivery", "latest_delivered_deepseek_fingerprint",
           "latest_sector_snapshot", "persist_analysis", "persist_delivery_outcome", "persist_index_samples",
           "persist_signal", "recent_discipline_events", "persist_quote_samples", "update_status"]
