"""Native-async, text-only projections for archived analyst evidence."""

from __future__ import annotations

from typing import Any


async def remote_reports(async_database: Any, limit: int, offset: int) -> dict[str, Any]:
    bounded_limit, bounded_offset = max(1, min(int(limit), 100)), max(0, int(offset))
    async with async_database.transaction() as connection:
        rows_result = await connection.execute(
            """SELECT r.remote_report_id,r.remote_analyst_id,a.name analyst_name,r.report_date,r.title,r.summary,r.remote_version,
                      r.content_hash,r.remote_published_at,r.first_synced_at,r.remote_updated_at,r.synced_at,r.mentioned_stocks,r.mentioned_sectors,r.predictions
               FROM quant.remote_reports r JOIN quant.remote_analysts a ON a.remote_analyst_id=r.remote_analyst_id
               ORDER BY r.report_date DESC,r.remote_updated_at DESC LIMIT %s OFFSET %s""", (bounded_limit, bounded_offset),
        )
        total_result = await connection.execute("SELECT count(*)::int total FROM quant.remote_reports")
        rows = [dict(row) for row in await rows_result.fetchall()]
        total_row = await total_result.fetchone()
    total = int((total_row or {}).get("total") or 0)
    return {"items": rows, "limit": bounded_limit, "offset": bounded_offset, "total": total,
            "next_offset": bounded_offset + len(rows) if bounded_offset + len(rows) < total else None}


async def remote_messages(async_database: Any, analyst_id: str | None, limit: int, offset: int) -> dict[str, Any]:
    bounded_limit, bounded_offset = max(1, min(int(limit), 100)), max(0, int(offset))
    async with async_database.transaction() as connection:
        rows_result = await connection.execute(
            """SELECT m.remote_message_id,m.remote_analyst_id,a.name analyst_name,m.source_item_id,m.source_message_id,m.source_entry_id,
                      m.source_type,m.content_hash,m.received_at,m.strategy_available_at,m.source_published_at,m.source_edited_at,
                      m.stated_at,m.stated_precision,m.time_evidence,m.remote_version,m.first_synced_at,m.synced_at,left(m.content,1000) content
                 FROM quant.remote_analyst_messages m JOIN quant.remote_analysts a ON a.remote_analyst_id=m.remote_analyst_id
                WHERE (%s::text IS NULL OR m.remote_analyst_id=%s)
                ORDER BY m.received_at DESC,m.remote_message_id DESC LIMIT %s OFFSET %s""",
            (analyst_id, analyst_id, bounded_limit, bounded_offset),
        )
        total_result = await connection.execute(
            "SELECT count(*)::int total FROM quant.remote_analyst_messages WHERE (%s::text IS NULL OR remote_analyst_id=%s)",
            (analyst_id, analyst_id),
        )
        rows = [dict(row) for row in await rows_result.fetchall()]
        total_row = await total_result.fetchone()
    total = int((total_row or {}).get("total") or 0)
    return {"items": rows, "analyst_id": analyst_id, "limit": bounded_limit, "offset": bounded_offset, "total": total,
            "next_offset": bounded_offset + len(rows) if bounded_offset + len(rows) < total else None}


async def analyst_claims(async_database: Any, limit: int, offset: int) -> dict[str, Any]:
    bounded_limit, bounded_offset = max(1, min(int(limit), 200)), max(0, int(offset))
    async with async_database.transaction() as connection:
        rows_result = await connection.execute(
            """SELECT c.claim_id,c.remote_analyst_id,a.name analyst_name,c.scope,c.subject_key,c.subject_label,c.direction,c.strength,
                      c.horizon_days,c.extraction_confidence,c.explicitness,c.published_at,c.available_at,e.remote_report_id,e.remote_message_id,e.evidence_key,
                      c.raw->>'direction_source' direction_source,left(e.body,500) evidence
               FROM quant.analyst_claims c JOIN quant.remote_analysts a ON a.remote_analyst_id=c.remote_analyst_id
               JOIN quant.analyst_evidence e ON e.evidence_id=c.evidence_id
               ORDER BY c.available_at DESC,c.created_at DESC LIMIT %s OFFSET %s""", (bounded_limit, bounded_offset),
        )
        total_result = await connection.execute("SELECT count(*)::int total FROM quant.analyst_claims")
        rows = [dict(row) for row in await rows_result.fetchall()]
        total_row = await total_result.fetchone()
    total = int((total_row or {}).get("total") or 0)
    return {"items": rows, "limit": bounded_limit, "offset": bounded_offset, "total": total,
            "next_offset": bounded_offset + len(rows) if bounded_offset + len(rows) < total else None}


async def claim_review_queue(async_database: Any, status: str, limit: int) -> dict[str, Any]:
    bounded_limit = max(1, min(int(limit), 300))
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT q.review_id,q.suggested_scope,q.suggested_label,q.suggested_symbol,q.direction,q.strength,q.horizon_days,
                      q.extraction_confidence,q.status,q.reviewer_note,q.created_at,e.remote_report_id,e.remote_message_id,left(e.body,500) evidence,
                      a.name analyst_name
               FROM quant.claim_review_queue q JOIN quant.analyst_evidence e ON e.evidence_id=q.evidence_id
               JOIN quant.analyst_claims c ON c.evidence_id=e.evidence_id
               JOIN quant.remote_analysts a ON a.remote_analyst_id=c.remote_analyst_id
               WHERE q.status=%s ORDER BY q.created_at DESC LIMIT %s""", (status, bounded_limit),
        )
        rows = [dict(row) for row in await result.fetchall()]
    return {"items": rows, "status": status}


__all__ = ["analyst_claims", "claim_review_queue", "remote_messages", "remote_reports"]
