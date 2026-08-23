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


async def remote_report_list_state(async_database: Any) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT a.remote_analyst_id,
                      count(DISTINCT r.remote_report_id)::int reports,max(r.report_date) latest_report_date,max(r.synced_at) last_report_synced_at,
                      count(DISTINCT m.remote_message_id)::int messages,max(m.received_at) latest_message_received_at,max(m.synced_at) last_message_synced_at
                 FROM quant.remote_analysts a
                 LEFT JOIN quant.remote_reports r ON r.remote_analyst_id=a.remote_analyst_id
                 LEFT JOIN quant.remote_analyst_messages m ON m.remote_analyst_id=a.remote_analyst_id
                GROUP BY a.remote_analyst_id ORDER BY a.remote_analyst_id"""
        )
        rows = [dict(row) for row in await result.fetchall()]
    return {"analysts": rows}


async def analyst_sync_cursor(async_database: Any, stream_key: str, analyst_id: str) -> dict[str, Any]:
    if stream_key not in {"messages", "reports"}:
        raise ValueError("stream_key must be messages or reports")
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT stream_key,remote_analyst_id,received_at,message_ids,report_versions,updated_at
                 FROM quant.analyst_sync_cursors WHERE stream_key=%s AND remote_analyst_id=%s""",
            (stream_key, analyst_id),
        )
        row = await result.fetchone()
    cursor = dict(row) if row else {"stream_key": stream_key, "remote_analyst_id": analyst_id,
                                    "received_at": None, "message_ids": [], "report_versions": {}, "updated_at": None}
    return {"cursor": cursor, **cursor}


async def analyst_global_sync_cursor(async_database: Any, stream_key: str) -> dict[str, Any]:
    if stream_key != "message_updates":
        raise ValueError("stream_key must be message_updates")
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT stream_key,remote_cursor,received_after,updated_at
                 FROM quant.analyst_global_sync_cursors WHERE stream_key=%s""", (stream_key,),
        )
        row = await result.fetchone()
    cursor = dict(row) if row else {
        "stream_key": stream_key, "remote_cursor": None, "received_after": None, "updated_at": None,
    }
    return {"cursor": cursor, **cursor}


__all__ = [
    "analyst_claims", "analyst_global_sync_cursor", "analyst_sync_cursor", "claim_review_queue",
    "remote_messages", "remote_report_list_state", "remote_reports",
]
