"""Native-async point-in-time analyst text-factor projection."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .analyst_text_features import DEFAULT_FACTOR_VERSION, summary_from_rows
from .remote_archive import classify_remote_text


async def analyst_text_factor_summary(
    async_database: Any, as_of_date: date, lookback_days: int = 7,
) -> dict[str, Any]:
    lookback_days = max(1, min(30, int(lookback_days)))
    earliest = as_of_date - timedelta(days=lookback_days - 1)
    async with async_database.transaction() as connection:
        reports_result = await connection.execute(
            """SELECT r.remote_analyst_id,r.remote_report_id,r.summary,r.sections,
                      r.first_synced_at AS available_at,
                      coalesce(r.remote_published_at,r.remote_updated_at,r.remote_created_at) AS published_at
                 FROM quant.remote_reports r
                WHERE (r.first_synced_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                ORDER BY available_at DESC""", (earliest, as_of_date),
        )
        claims_result = await connection.execute(
            """SELECT c.remote_analyst_id,c.subject_key,c.subject_label,c.direction,c.strength,c.extraction_confidence,
                      c.available_at,e.remote_report_id,e.evidence_key
                 FROM quant.analyst_claims c JOIN quant.analyst_evidence e ON e.evidence_id=c.evidence_id
                WHERE c.scope='theme' AND (c.available_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                  AND e.evidence_key = ANY(%s)""",
            (earliest, as_of_date, ["summary", "section:market_view", "section:operation_guidance",
                                     "section:future_scenarios", "section:sectors_and_stocks"]),
        )
        reports = [dict(row) for row in await reports_result.fetchall()]
        topic_claims = [dict(row) for row in await claims_result.fetchall()]
    return summary_from_rows(
        reports, topic_claims, as_of_date, classify_text=classify_remote_text,
        factor_version=DEFAULT_FACTOR_VERSION, lookback_days=lookback_days,
    )


__all__ = ["analyst_text_factor_summary"]
