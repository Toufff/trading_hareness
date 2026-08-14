"""Point-in-time safe manual review of report and message claims."""

from __future__ import annotations

import re
from typing import Any, Callable

from fastapi import HTTPException
from psycopg.types.json import Json


def review_claim(
    review_id: Any,
    payload: Any,
    *,
    database: Any,
    exchange_for: Callable[[str], str],
) -> dict[str, Any]:
    """Approve/reject one queue row without changing its availability time.

    An evidence row has exactly one source, but that source can now be either
    a daily remote report or a received remote message.  Manual approval must
    inherit the evidence's immutable available_at; using ``now()`` would turn
    a delayed review into an erroneous point-in-time trading observation.
    """
    with database.transaction() as connection:
        item = connection.execute(
            """SELECT q.*,e.available_at,COALESCE(r.remote_analyst_id,m.remote_analyst_id) AS remote_analyst_id
               FROM quant.claim_review_queue q
               JOIN quant.analyst_evidence e ON e.evidence_id=q.evidence_id
               LEFT JOIN quant.remote_reports r ON r.remote_report_id=e.remote_report_id
               LEFT JOIN quant.remote_analyst_messages m ON m.remote_message_id=e.remote_message_id
               WHERE q.review_id=%s FOR UPDATE""",
            (review_id,),
        ).fetchone()
        if not item:
            raise HTTPException(status_code=404, detail="review item not found")
        if item["status"] != "pending":
            raise HTTPException(status_code=409, detail="review item was already decided")
        if not item["remote_analyst_id"] or item["available_at"] is None:
            raise HTTPException(status_code=422, detail="review evidence has no immutable analyst availability")
        if payload.status == "approved":
            symbol = (payload.symbol or item["suggested_symbol"] or "").upper()
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
                raise HTTPException(status_code=422, detail="approving a stock claim requires a Tushare symbol")
            connection.execute(
                "INSERT INTO quant.instruments(symbol,exchange,source) VALUES(%s,%s,'claim-review') ON CONFLICT(symbol) DO NOTHING",
                (symbol, exchange_for(symbol)),
            )
            connection.execute(
                """INSERT INTO quant.analyst_claims(evidence_id,remote_analyst_id,scope,subject_key,subject_label,direction,strength,
                      horizon_days,extraction_confidence,extractor_version,available_at,raw)
                   VALUES(%s,%s,'stock',%s,%s,%s,%s,%s,%s,'manual-claim-review-v1',%s,%s)
                   ON CONFLICT(evidence_id,scope,subject_key,horizon_days,extractor_version) DO UPDATE SET direction=EXCLUDED.direction,
                      strength=EXCLUDED.strength,extraction_confidence=EXCLUDED.extraction_confidence,
                      available_at=EXCLUDED.available_at""",
                (item["evidence_id"], item["remote_analyst_id"], symbol, item["suggested_label"], item["direction"], item["strength"],
                 item["horizon_days"], item["extraction_confidence"], item["available_at"],
                 Json({"review_id": str(review_id), "reviewer_note": payload.reviewer_note})),
            )
        connection.execute(
            "UPDATE quant.claim_review_queue SET status=%s,reviewed_at=now(),reviewer_note=%s WHERE review_id=%s",
            (payload.status, payload.reviewer_note, review_id),
        )
    return {"review_id": str(review_id), "status": payload.status}


__all__ = ["review_claim"]
