"""Narrow evidence boundary using the existing source-keyed flow store."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from psycopg.types.json import Json

SOURCE = "longhuvip_main_net"


def coverage(database: Any, day: date) -> int:
    with database.transaction() as c:
        row = c.execute("""SELECT count(*)::int AS n FROM quant.stock_money_flow_daily
            WHERE trading_date=%s AND source=%s AND raw ? 'screen_snapshot'""", (day, SOURCE)).fetchone()
    return row["n"]


def persist(database: Any, day: date, rows: dict[str, dict], health: dict) -> int:
    now = datetime.now(timezone.utc)
    with database.transaction() as c:
        known = {r["symbol"] for r in c.execute("SELECT symbol FROM quant.instruments").fetchall()}
        values = []
        for symbol, row in rows.items():
            if symbol not in known:
                continue
            snapshot = {k: v for k, v in row.items() if k != "raw"}
            values.append((symbol, day, SOURCE, row["main_net"], now,
                           Json({"screen_snapshot": snapshot, "plate_id": row["plate_id"],
                                 "flow_convention": row["flow_convention"],
                                 "screen_received_at": now.isoformat(),
                                 "screen_source_health": {"plate_coverage": health.get("plate_coverage"), "physical_page_limit": 300}})))
        with c.cursor() as cur:
            cur.executemany("""INSERT INTO quant.stock_money_flow_daily
                (symbol,trading_date,source,provider,net_amount,available_at,raw)
                VALUES(%s,%s,%s,'longhuvip_composite',%s,%s,%s)
                ON CONFLICT(symbol,trading_date,source) DO UPDATE SET
                  net_amount=EXCLUDED.net_amount,raw=quant.stock_money_flow_daily.raw || EXCLUDED.raw,
                  available_at=greatest(quant.stock_money_flow_daily.available_at,EXCLUDED.available_at)""", values)
    return len(values)


def load(database: Any, day: date) -> tuple[list[dict], list[str]]:
    with database.transaction() as c:
        dates = c.execute("""SELECT trading_date FROM quant.stock_money_flow_daily
            WHERE trading_date<=%s AND trading_date>=%s AND source=%s AND raw ? 'screen_snapshot'
            GROUP BY trading_date HAVING count(*)>=1000 ORDER BY trading_date DESC LIMIT 11""",
                          (day, day-timedelta(days=35), SOURCE)).fetchall()
        sessions = sorted(str(r["trading_date"]) for r in dates)
        if not sessions:
            return [], []
        rows = c.execute("""SELECT raw->'screen_snapshot' AS snapshot FROM quant.stock_money_flow_daily
            WHERE trading_date=ANY(%s::date[]) AND source=%s AND raw ? 'screen_snapshot'""", (sessions, SOURCE)).fetchall()
    return [dict(r["snapshot"]) for r in rows], sessions


def verified_events(database: Any, day: date) -> dict[str, list[dict]]:
    """Raw news titles are not automatically verified company benefits.

    Only explicitly reviewed structured evidence can enter the event lane.
    Existing event text stays searchable but is not relabelled as validation.
    """
    with database.transaction() as c:
        rows = c.execute("""SELECT symbol,title,url,body,event_type,source,occurred_at,available_at FROM quant.market_events
            WHERE event_type='short_term_verified_catalyst' AND occurred_at::date BETWEEN %s AND %s
              AND available_at < (%s::date+1) AND source='primary_review'
            ORDER BY occurred_at DESC""", (day-timedelta(days=7), day, day)).fetchall()
    return _events(rows)


def _events(rows: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for row in rows:
        result.setdefault(row["symbol"], []).append({
            "verified": True, "title": row["title"], "url": row["url"], "benefit": row["body"],
            "event_type": row.get("event_type") or "short_term_verified_catalyst",
            "source": row.get("source") or "primary_review",
            "published_date": row["occurred_at"].date().isoformat(),
            "available_at": row["available_at"].isoformat() if row.get("available_at") else None,
            "surprise": "unknown", "priced_in": "unknown", "impact_direction": "unknown",
        })
    return result
