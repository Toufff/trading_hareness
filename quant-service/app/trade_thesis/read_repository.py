"""Native async, read-only projections for the trade-thesis HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any


async def _rows(async_database: Any, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    async with async_database.transaction() as connection:
        result = await connection.execute(sql, params)
        return [dict(row) for row in await result.fetchall()]


async def list_theses(database: Any, *, symbol: str | None = None,
                      as_of: datetime | None = None, namespace: str = "shadow",
                      limit: int = 200) -> list[dict[str, Any]]:
    return await _rows(database, """
        WITH events AS (
          SELECT r.*, row_number() OVER (PARTITION BY thesis_id ORDER BY content_revision DESC,event_seq DESC) AS n
            FROM quant.trade_thesis_revisions r
           WHERE event_type IN ('capture','approve') AND (%s::text IS NULL OR symbol=%s)
             AND (event_type='capture' OR NOT EXISTS (
                 SELECT 1 FROM quant.trade_thesis_revisions conflict
                  WHERE conflict.thesis_id=r.thesis_id AND conflict.content_revision=r.content_revision
                    AND conflict.proposal_hash=r.proposal_hash
                    AND conflict.event_type IN ('reject','needs_evidence')))
             AND (%s::timestamptz IS NULL OR created_at<=%s)
        ), latest_eval AS (
          SELECT DISTINCT ON (e.thesis_id) e.thesis_id,e.result,e.created_at
            FROM quant.trade_thesis_evaluations e
           WHERE e.namespace=%s AND (%s::timestamptz IS NULL OR (e.cutoff_at<=%s AND e.created_at<=%s))
           ORDER BY e.thesis_id,e.cutoff_at DESC,e.created_at DESC
        )
        SELECT events.thesis_id,events.symbol,events.content_revision AS revision,
               events.payload AS thesis,latest_eval.result AS evaluation,
               events.created_at,latest_eval.created_at AS evaluated_at
          FROM events LEFT JOIN latest_eval USING(thesis_id) WHERE events.n=1
         ORDER BY events.created_at DESC LIMIT %s
    """, (symbol, symbol, as_of, as_of, namespace, as_of, as_of, as_of,
          max(1, min(limit, 500))))


async def thesis_timeline(database: Any, thesis_id: str, *, as_of: datetime | None = None,
                          namespace: str = "shadow", limit: int = 500) -> dict[str, Any] | None:
    if not thesis_id or len(thesis_id) > 200:
        return None
    revisions = await _rows(database, """
        SELECT event_id,event_seq,thesis_id,symbol,event_type,content_revision AS revision,
               base_revision,payload,actor,proposal_hash,review_data,content_hash,created_at
          FROM quant.trade_thesis_revisions WHERE thesis_id=%s AND (%s::timestamptz IS NULL OR created_at<=%s)
         ORDER BY event_seq,created_at LIMIT %s
    """, (thesis_id, as_of, as_of, max(1, min(limit, 1000))))
    if not revisions:
        return None
    evaluations = await _rows(database, """
        SELECT evaluation_id,thesis_revision,source_run_id,cutoff_at,namespace,result,content_hash,created_at
          FROM quant.trade_thesis_evaluations
         WHERE thesis_id=%s AND namespace=%s
           AND (%s::timestamptz IS NULL OR (cutoff_at<=%s AND created_at<=%s))
         ORDER BY cutoff_at,created_at LIMIT %s
    """, (thesis_id, namespace, as_of, as_of, as_of, max(1, min(limit, 1000))))
    return {"thesis_id": thesis_id, "revisions": revisions, "evaluations": evaluations}


__all__ = ["list_theses", "thesis_timeline"]
