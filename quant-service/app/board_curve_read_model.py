"""Read-only projections of stored one-minute Eastmoney board-flow evidence."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from statistics import median
from typing import Any, Literal
from zoneinfo import ZoneInfo


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def board_display_slots(selected_date: date, now: datetime | None = None) -> list[datetime]:
    """Return elapsed SSE-minute slots in UTC, independent of the browser clock."""
    china = ZoneInfo("Asia/Shanghai")
    local_now = (now or datetime.now(timezone.utc)).astimezone(china)
    if selected_date > local_now.date():
        return []
    end_time = time(15, 0)
    if selected_date == local_now.date():
        current = local_now.time()
        if current < time(9, 20):
            return []
        if current <= time(11, 30):
            end_time = time(current.hour, current.minute)
        elif current < time(13, 0):
            end_time = time(11, 30)
        elif current <= time(15, 0):
            end_time = time(current.hour, current.minute)

    def minute_range(start: time, end: time) -> list[datetime]:
        cursor = datetime.combine(selected_date, start, tzinfo=china)
        boundary = datetime.combine(selected_date, end, tzinfo=china)
        output: list[datetime] = []
        while cursor <= boundary:
            output.append(cursor.astimezone(timezone.utc))
            cursor += timedelta(minutes=1)
        return output

    slots = minute_range(time(9, 20), min(end_time, time(11, 30)))
    if end_time >= time(13, 0):
        slots.extend(minute_range(time(13, 0), end_time))
    return slots


def latest_close_sector_review_report(database: Any) -> dict[str, Any]:
    """Return the latest persisted board review without triggering a refresh."""
    with database.transaction() as connection:
        row = connection.execute(
            """SELECT board_report_id,observed_at,status,source_status,summary,payload,created_at
                 FROM quant.intraday_board_reports ORDER BY observed_at DESC LIMIT 1"""
        ).fetchone()
    return project_latest_close_sector_review_report(row)


def project_latest_close_sector_review_report(row: Any) -> dict[str, Any]:
    """Project one already-read board review without a database dependency."""
    return {
        "report": row,
        "notice": "板块 Top10 是同花顺精确成员与同一腾讯横截面的已保存复盘证据。",
    }


def intraday_board_flow_curves(
    database: Any,
    trade_date: date | None = None,
    taxonomy: Literal["industry", "concept"] = "industry",
    since: datetime | None = None,
    *,
    curve_retention_days: int,
    rotation_retention_days: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read bounded local board curves; never refresh or backfill a provider."""
    exchange_now = now or datetime.now(timezone.utc)
    china = ZoneInfo("Asia/Shanghai")
    selected_date = trade_date or exchange_now.astimezone(china).date()
    window_start = datetime.combine(selected_date, time(9, 20), tzinfo=china).astimezone(timezone.utc)
    window_end = datetime.combine(selected_date, time(15, 1), tzinfo=china).astimezone(timezone.utc)
    if since is not None:
        since = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since.astimezone(timezone.utc)
    values: tuple[Any, ...] = (window_start, window_end, since, since)
    with database.transaction() as connection:
        curve_rows = connection.execute(
            """SELECT observed_at,status,coverage,payload,'minute_curve' AS source
                 FROM quant.intraday_board_flow_snapshots
                WHERE observed_at>=%s AND observed_at<%s AND status IN ('completed','partial')
                  AND (%s::timestamptz IS NULL OR observed_at>%s)
                ORDER BY observed_at LIMIT 720""",
            values,
        ).fetchall()
        legacy_rows = connection.execute(
            """SELECT observed_at,status,payload->'coverage' AS coverage,payload,'strategy_report' AS source
                 FROM quant.intraday_board_reports
                WHERE observed_at>=%s AND observed_at<%s AND status IN ('completed','partial')
                  AND (%s::timestamptz IS NULL OR observed_at>%s)
                ORDER BY observed_at LIMIT 720""",
            values,
        ).fetchall()
    return project_intraday_board_flow_curves(
        curve_rows, legacy_rows, trade_date=trade_date, taxonomy=taxonomy, since=since,
        curve_retention_days=curve_retention_days, rotation_retention_days=rotation_retention_days, now=exchange_now,
    )


def project_intraday_board_flow_curves(
    curve_rows: list[Any],
    legacy_rows: list[Any],
    *,
    trade_date: date | None,
    taxonomy: Literal["industry", "concept"],
    since: datetime | None,
    curve_retention_days: int,
    rotation_retention_days: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project bounded stored board rows; callers own all database I/O."""
    exchange_now = now or datetime.now(timezone.utc)
    china = ZoneInfo("Asia/Shanghai")
    selected_date = trade_date or exchange_now.astimezone(china).date()
    taxonomy_key = f"eastmoney_{taxonomy}"

    selected_by_minute: dict[datetime, dict[str, Any]] = {}
    all_rows: list[dict[str, Any]] = []
    for raw_row in (*legacy_rows, *curve_rows):
        row = dict(raw_row)
        local_time = row["observed_at"].astimezone(china).time()
        if time(9, 20) <= local_time <= time(11, 30, 59) or time(13, 0) <= local_time <= time(15, 0, 59):
            all_rows.append(row)
    for row in all_rows:
        local_minute = row["observed_at"].astimezone(china).replace(second=0, microsecond=0)
        coverage = dict(row.get("coverage") or {})
        score = int((coverage.get(taxonomy) or {}).get("flow_boards") or 0)
        priority = 1 if row["source"] == "minute_curve" else 0
        current = selected_by_minute.get(local_minute)
        if current is None or (score, priority, row["observed_at"]) > current["rank"]:
            selected_by_minute[local_minute] = {**row, "rank": (score, priority, row["observed_at"]), "taxonomy_coverage": score}

    series: dict[tuple[str, str], dict[str, Any]] = {}
    snapshots: list[dict[str, Any]] = []
    for local_minute, row in sorted(selected_by_minute.items()):
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in list((dict(row.get("payload") or {})).get("items") or []):
            if item.get("taxonomy_key") != taxonomy_key or item.get("net_inflow") is None:
                continue
            sector_key, label = str(item.get("sector_key") or "").strip(), str(item.get("label") or "").strip()
            if sector_key and label:
                grouped.setdefault((sector_key, label), []).append(item)
        point_time = local_minute.astimezone(timezone.utc).isoformat()
        for key, items in grouped.items():
            net_values = [float(item["net_inflow"]) for item in items if _number(item.get("net_inflow")) is not None]
            change_values = [float(item["change_pct"]) for item in items if _number(item.get("change_pct")) is not None]
            if not net_values:
                continue
            item = series.setdefault(key, {"taxonomy_key": taxonomy_key, "sector_key": key[0], "label": key[1], "points": []})
            item["points"].append({
                "observed_at": point_time, "net_inflow": round(median(net_values), 6),
                "change_pct": round(median(change_values), 6) if change_values else None,
            })
        snapshots.append({"observed_at": point_time, "coverage": row["taxonomy_coverage"], "source": row["source"]})

    items = list(series.values())
    items.sort(key=lambda item: (-abs(float(item["points"][-1]["net_inflow"])) if item["points"] else 0, str(item["label"])))
    cursor = max((row["observed_at"] for row in all_rows), default=since)
    display_slots = board_display_slots(selected_date, exchange_now)
    return {
        "trade_date": str(selected_date), "timezone": "Asia/Shanghai", "taxonomy": taxonomy,
        "taxonomy_key": taxonomy_key, "items": items, "snapshots": snapshots,
        "cursor": cursor.isoformat() if cursor else None,
        "exchange_clock_observed_at": exchange_now.isoformat(),
        "is_exchange_today": selected_date == exchange_now.astimezone(china).date(),
        "display_slots": [slot.isoformat() for slot in display_slots],
        "display_start": display_slots[0].isoformat() if display_slots else None,
        "display_end": display_slots[-1].isoformat() if display_slots else None,
        "cadence_seconds": 60, "retention_days": curve_retention_days,
        "rotation_retention_days": rotation_retention_days,
        "source": "eastmoney_instant_board_flow",
        "notice": "时间轴由上交所时钟生成：09:20 起每分钟增量追加。可视化缺口使用最近真实值补点并明确标注，原始快照不改写、不补零。策略 Top10 仍使用独立完整报告。",
    }


__all__ = [
    "board_display_slots", "intraday_board_flow_curves", "latest_close_sector_review_report",
    "project_intraday_board_flow_curves", "project_latest_close_sector_review_report",
]
