"""Single, fail-closed live gate for analyst-derived context."""

from __future__ import annotations

from datetime import date
from typing import Any


PROMOTION_KEY = "analyst_delta"
MAX_APPROVED_WEIGHT = 0.10


def analyst_live_promotion(connection: Any, as_of_date: date) -> dict[str, Any]:
    """Return the only allowed analyst live-weight decision.

    Historical scorecards and descriptive skill cards deliberately have no
    authority here.  A failed/missing registry row is equivalent to zero.
    """
    row = connection.execute(
        """SELECT methodology_version,status,approved_by,approved_at,max_live_weight,reason,evidence
             FROM quant.analyst_promotion_registry WHERE promotion_key=%s""",
        (PROMOTION_KEY,),
    ).fetchone()
    if row is None:
        return {"execution_eligible": False, "weight": 0.0, "reason": "promotion_registry_missing",
                "promotion_key": PROMOTION_KEY, "as_of_date": str(as_of_date)}
    item = dict(row)
    approved = (item.get("status") == "approved" and item.get("approved_by") and item.get("approved_at"))
    weight = min(MAX_APPROVED_WEIGHT, max(0.0, float(item.get("max_live_weight") or 0))) if approved else 0.0
    return {"execution_eligible": bool(weight > 0), "weight": weight,
            "reason": str(item.get("reason") or ("approved" if weight else "promotion_not_approved")),
            "promotion_key": PROMOTION_KEY, "methodology_version": item.get("methodology_version"),
            "status": item.get("status"), "approved_by": item.get("approved_by"),
            "approved_at": item.get("approved_at"), "as_of_date": str(as_of_date),
            "evidence": item.get("evidence") or {}}


__all__ = ["PROMOTION_KEY", "analyst_live_promotion"]
