"""Bounded, text-only read models for archived analyst research."""

from __future__ import annotations

from typing import Any


def remote_reports(database: Any, limit: int, offset: int) -> dict[str, Any]:
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT r.remote_report_id,r.remote_analyst_id,a.name analyst_name,r.report_date,r.title,r.summary,r.remote_version,
                      r.content_hash,r.remote_published_at,r.first_synced_at,r.remote_updated_at,r.synced_at,r.mentioned_stocks,r.mentioned_sectors,r.predictions
               FROM quant.remote_reports r JOIN quant.remote_analysts a ON a.remote_analyst_id=r.remote_analyst_id
               ORDER BY r.report_date DESC,r.remote_updated_at DESC LIMIT %s OFFSET %s""", (limit, offset),
        ).fetchall()
        total = connection.execute("SELECT count(*)::int total FROM quant.remote_reports").fetchone()["total"]
    return {"items": rows, "limit": limit, "offset": offset, "total": total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None}


def remote_messages(database: Any, analyst_id: str | None, limit: int, offset: int) -> dict[str, Any]:
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT m.remote_message_id,m.remote_analyst_id,a.name analyst_name,m.source_item_id,m.source_message_id,m.source_entry_id,
                      m.source_type,m.content_hash,m.received_at,m.strategy_available_at,m.source_published_at,m.source_edited_at,
                      m.stated_at,m.stated_precision,m.time_evidence,m.remote_version,m.first_synced_at,m.synced_at,left(m.content,1000) content
                 FROM quant.remote_analyst_messages m JOIN quant.remote_analysts a ON a.remote_analyst_id=m.remote_analyst_id
                WHERE (%s::text IS NULL OR m.remote_analyst_id=%s)
                ORDER BY m.received_at DESC,m.remote_message_id DESC LIMIT %s OFFSET %s""",
            (analyst_id, analyst_id, limit, offset),
        ).fetchall()
        total = connection.execute(
            "SELECT count(*)::int total FROM quant.remote_analyst_messages WHERE (%s::text IS NULL OR remote_analyst_id=%s)",
            (analyst_id, analyst_id),
        ).fetchone()["total"]
    return {"items": rows, "analyst_id": analyst_id, "limit": limit, "offset": offset, "total": total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None}


def analyst_claims(database: Any, limit: int, offset: int) -> dict[str, Any]:
    limit, offset = max(1, min(limit, 200)), max(0, offset)
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT c.claim_id,c.remote_analyst_id,a.name analyst_name,c.scope,c.subject_key,c.subject_label,c.direction,c.strength,
                      c.horizon_days,c.extraction_confidence,c.explicitness,c.published_at,c.available_at,e.remote_report_id,e.remote_message_id,e.evidence_key,
                      c.raw->>'direction_source' direction_source,left(e.body,500) evidence
               FROM quant.analyst_claims c JOIN quant.remote_analysts a ON a.remote_analyst_id=c.remote_analyst_id
               JOIN quant.analyst_evidence e ON e.evidence_id=c.evidence_id
               ORDER BY c.available_at DESC,c.created_at DESC LIMIT %s OFFSET %s""", (limit, offset),
        ).fetchall()
        total = connection.execute("SELECT count(*)::int total FROM quant.analyst_claims").fetchone()["total"]
    return {"items": rows, "limit": limit, "offset": offset, "total": total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None}


def claim_review_queue(database: Any, status: str, limit: int) -> dict[str, Any]:
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT q.review_id,q.suggested_scope,q.suggested_label,q.suggested_symbol,q.direction,q.strength,q.horizon_days,
                      q.extraction_confidence,q.status,q.reviewer_note,q.created_at,e.remote_report_id,e.remote_message_id,left(e.body,500) evidence,
                      a.name analyst_name
               FROM quant.claim_review_queue q JOIN quant.analyst_evidence e ON e.evidence_id=q.evidence_id
               JOIN quant.analyst_claims c ON c.evidence_id=e.evidence_id
               JOIN quant.remote_analysts a ON a.remote_analyst_id=c.remote_analyst_id
               WHERE q.status=%s ORDER BY q.created_at DESC LIMIT %s""", (status, max(1, min(limit, 300))),
        ).fetchall()
    return {"items": rows, "status": status}
