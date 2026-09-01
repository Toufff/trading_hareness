"""Refresh only the daily inputs needed by the always-on intraday edge."""

from __future__ import annotations

import calendar
from datetime import datetime
import os
import sys
from zoneinfo import ZoneInfo

import httpx
import psycopg


BASE_URL = "http://127.0.0.1:18110"


def post(client: httpx.Client, path: str, payload: dict[str, object]) -> dict[str, object]:
    response = client.post(path, json=payload)
    response.raise_for_status()
    body = response.json()
    if isinstance(body, dict) and body.get("status") in {"failed", "blocked"}:
        raise RuntimeError(f"{path} returned {body.get('status')}: {body.get('reason') or body.get('error')}")
    return body


def current_trade_date() -> str | None:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    with psycopg.connect(
        host=os.getenv("PGHOST", "/var/run/postgresql"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "quant_intraday_edge"),
        user=os.getenv("PGUSER", "quant_edge"),
    ) as connection:
        row = connection.execute(
            "SELECT is_open FROM quant.market_trade_calendar WHERE exchange='SSE' AND calendar_date=%s",
            (today,),
        ).fetchone()
    return today.isoformat() if row and bool(row[0]) else None


def prune_edge_evidence() -> dict[str, int]:
    """Keep the edge bounded; the workstation remains the research archive."""
    statements = {
        "tushare_raw_records": "DELETE FROM quant.tushare_raw_records WHERE available_at < now()-interval '14 days'",
        "raw_market_observations": "DELETE FROM quant.raw_market_observations WHERE available_at < now()-interval '14 days'",
        "daily_adjustment_factors": "DELETE FROM quant.daily_adjustment_factors WHERE trading_date < current_date-interval '180 days'",
        "daily_fundamentals": "DELETE FROM quant.daily_fundamentals WHERE trading_date < current_date-interval '180 days'",
        "daily_trade_limits": "DELETE FROM quant.daily_trade_limits WHERE trading_date < current_date-interval '180 days'",
        "market_bars_daily": "DELETE FROM quant.market_bars_daily WHERE trading_date < current_date-interval '180 days'",
        "canonical_bars_daily": "DELETE FROM quant.canonical_bars_daily WHERE trading_date < current_date-interval '180 days'",
        "fetch_runs": """DELETE FROM quant.fetch_runs run WHERE run.created_at < now()-interval '30 days'
                           AND NOT EXISTS (SELECT 1 FROM quant.raw_market_observations raw WHERE raw.fetch_run_id=run.fetch_run_id)""",
    }
    deleted: dict[str, int] = {}
    with psycopg.connect(
        host=os.getenv("PGHOST", "/var/run/postgresql"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "quant_intraday_edge"),
        user=os.getenv("PGUSER", "quant_edge"),
    ) as connection:
        for table, statement in statements.items():
            deleted[table] = int(connection.execute(statement).rowcount or 0)
    return deleted


def main() -> int:
    write_key = os.getenv("QUANT_WRITE_API_KEY", "").strip()
    if not write_key:
        raise RuntimeError("QUANT_WRITE_API_KEY is required")
    headers = {"X-Quant-Write-Key": write_key}
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    month_start = now.date().replace(day=1).strftime("%Y%m%d")
    month_end = now.date().replace(day=calendar.monthrange(now.year, now.month)[1]).strftime("%Y%m%d")
    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=600.0, trust_env=False) as client:
        calendar_result = post(client, "/api/v1/providers/tushare/fetch", {
            "api_name": "trade_cal", "provider": "auto", "max_rows": 40,
            "params": {"exchange": "SSE", "start_date": month_start, "end_date": month_end},
            "force_refresh": True,
        })
        # Retention is independent of the control refresh. A blocked provider
        # response must not leave old edge evidence occupying the hot budget.
        retention = prune_edge_evidence()
        trade_date = current_trade_date()
        if trade_date is None:
            print({
                "status": "closed", "calendar": calendar_result.get("status"),
                "observed_at": now.isoformat(), "retention": retention,
            })
            return 0
        daily = post(client, "/api/v1/market/sync/full-daily", {
            "trade_date": trade_date, "provider": "auto", "minimum_rows": 5000,
        })
        controls = post(client, "/api/v1/market/sync/full-daily-controls", {"trade_date": trade_date})
        print({
            "status": "completed", "trade_date": trade_date,
            "daily_status": daily.get("status"), "daily_rows": daily.get("imported_rows"),
            "controls_status": controls.get("status"), "retention": retention,
        })
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # systemd records a bounded operator-facing failure
        print(f"intraday edge daily refresh failed: {str(error)[:500]}", file=sys.stderr)
        raise
