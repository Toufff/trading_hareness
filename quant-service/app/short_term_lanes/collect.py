"""Incremental close-history acquisition through the existing <=300 wrapper.

Never manufacture OHLC from ranking closes. These source-specific snapshots
live in the existing flow evidence JSON, not canonical daily-bar tables.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any

from ..longhu_vendor_source import LonghuVendorSource
from .repository import coverage, persist


def collect(database: Any, day: date, *, client: Any = None, log=print, required_sessions: int = 11) -> dict:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if day > now.date() or day == now.date() and now.hour < 16:
        raise ValueError("settled history is collected only after 16:00 Shanghai")
    client = client or LonghuVendorSource()
    catalog = client.industry_plate_catalog()
    # Use the vendor's catalogue contract; do not create an alternative API.
    plates = [r["sector_key"] for r in catalog]
    labels = {str(r.get("plate_id") or r.get("code") or r.get("sector_key")): r.get("name") or r.get("label") for r in catalog}
    done, records, offset = [], [], 0
    with database.transaction() as c:
        calendar = {str(r["calendar_date"]): r["is_open"] for r in c.execute(
            "SELECT calendar_date,is_open FROM quant.market_trade_calendar WHERE exchange='SSE' AND calendar_date BETWEEN %s AND %s",
            (day-timedelta(days=35), day)).fetchall()}
    while len(done) < required_sessions and offset <= 35:
        target = day-timedelta(days=offset); offset += 1
        if target.weekday() >= 5 or calendar.get(str(target)) is False:
            continue
        existing = coverage(database, target)
        if existing >= 4800:
            done.append(str(target)); records.append({"date": str(target), "status": "cached", "rows": existing}); continue
        rows, health = client.full_market_vendor_rows(target, plate_ids=plates)
        failed_plates = [str(item["plate_id"]) for item in health.get("errors", [])]
        if failed_plates:
            retry_rows, retry_health = client.full_market_vendor_rows(target, plate_ids=failed_plates)
            rows.update(retry_rows)
            remaining = len(retry_health.get("errors", []))
            health = {**health, "plate_coverage": (len(plates)-remaining)/len(plates),
                      "errors": retry_health.get("errors", []),
                      "duplicate_conflicts": [*health.get("duplicate_conflicts", []), *retry_health.get("duplicate_conflicts", [])]}
        if health.get("plate_coverage", 0) < 0.99 or len(rows) < 4800 or health.get("duplicate_conflicts"):
            log({"date": str(target), "status": "failed", "symbols": len(rows), "plate_coverage": health.get("plate_coverage")})
            # An empty holiday is not silently invented as a session. An
            # unknown/missing weekday stops the scan rather than dropping it.
            raise RuntimeError(f"Incomplete Longhu cross-section for {target}: {len(rows)} rows")
        for row in rows.values():
            row["sector_label"] = labels.get(str(row["plate_id"])) or row["plate_id"]
        count = persist(database, target, rows, health)
        done.append(str(target)); item = {"date": str(target), "status": "stored", "rows": count}
        records.append(item); log(item)
    return {"status": "completed" if len(done) == required_sessions else "incomplete", "sessions": sorted(done), "records": records}
