"""Native-async reads for persisted one-minute board-flow evidence."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .board_curve_read_model import (
    project_intraday_board_flow_curves,
    project_latest_close_sector_review_report,
)


async def latest_close_sector_review_report(async_database: Any) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT board_report_id,observed_at,status,source_status,summary,payload,created_at
                 FROM quant.intraday_board_reports ORDER BY observed_at DESC LIMIT 1"""
        )
        row = await result.fetchone()
    return project_latest_close_sector_review_report(dict(row) if row else None)


async def intraday_board_flow_curves(
    async_database: Any,
    trade_date: date | None = None,
    taxonomy: Literal["industry", "concept"] = "industry",
    since: datetime | None = None,
    *,
    curve_retention_days: int,
    rotation_retention_days: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read at most one trading day's local rows; never refresh a provider."""
    exchange_now = now or datetime.now(timezone.utc)
    china = ZoneInfo("Asia/Shanghai")
    selected_date = trade_date or exchange_now.astimezone(china).date()
    window_start = datetime.combine(selected_date, time(9, 20), tzinfo=china).astimezone(timezone.utc)
    window_end = datetime.combine(selected_date, time(15, 1), tzinfo=china).astimezone(timezone.utc)
    normalized_since = since
    if normalized_since is not None:
        normalized_since = normalized_since.replace(tzinfo=timezone.utc) if normalized_since.tzinfo is None else normalized_since.astimezone(timezone.utc)
    values: tuple[Any, ...] = (window_start, window_end, normalized_since, normalized_since)
    async with async_database.transaction() as connection:
        curves_result = await connection.execute(
            """SELECT observed_at,status,coverage,payload,'minute_curve' AS source
                 FROM quant.intraday_board_flow_snapshots
                WHERE observed_at>=%s AND observed_at<%s AND status IN ('completed','partial')
                  AND (%s::timestamptz IS NULL OR observed_at>%s)
                ORDER BY observed_at LIMIT 720""",
            values,
        )
        legacy_result = await connection.execute(
            """SELECT observed_at,status,payload->'coverage' AS coverage,payload,'strategy_report' AS source
                 FROM quant.intraday_board_reports
                WHERE observed_at>=%s AND observed_at<%s AND status IN ('completed','partial')
                  AND (%s::timestamptz IS NULL OR observed_at>%s)
                ORDER BY observed_at LIMIT 720""",
            values,
        )
        curve_rows = [dict(row) for row in await curves_result.fetchall()]
        legacy_rows = [dict(row) for row in await legacy_result.fetchall()]
    return project_intraday_board_flow_curves(
        curve_rows, legacy_rows, trade_date=trade_date, taxonomy=taxonomy, since=normalized_since,
        curve_retention_days=curve_retention_days, rotation_retention_days=rotation_retention_days, now=exchange_now,
    )


__all__ = ["intraday_board_flow_curves", "latest_close_sector_review_report"]
