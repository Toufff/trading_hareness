"""Single, fail-closed live gate for research/shadow strategy contracts.

``platform.strategy_registry.STRATEGY_CONTRACTS`` declares each strategy's
model/input identity and a static ``live_effect`` that is always ``"none"``
in source. That registry alone cannot approve anything: it is a code-review
artifact, not an audit trail. This module is the only place a strategy's
live weight can become nonzero, mirroring ``analyst_promotion.py`` exactly -
a missing or unapproved registry row is equivalent to zero.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .platform.strategy_registry import STRATEGY_CONTRACTS

MAX_APPROVED_WEIGHT = 0.10


def strategy_live_promotion(connection: Any, strategy_key: str, as_of_date: date) -> dict[str, Any]:
    """Return the only allowed live-weight decision for one strategy contract."""
    row = connection.execute(
        """SELECT methodology_version,status,approved_by,approved_at,max_live_weight,reason,evidence
             FROM quant.strategy_promotion_registry WHERE strategy_key=%s""",
        (strategy_key,),
    ).fetchone()
    if row is None:
        return {"execution_eligible": False, "weight": 0.0, "reason": "promotion_registry_missing",
                "strategy_key": strategy_key, "as_of_date": str(as_of_date)}
    item = dict(row)
    approved = (item.get("status") == "approved" and item.get("approved_by") and item.get("approved_at"))
    weight = min(MAX_APPROVED_WEIGHT, max(0.0, float(item.get("max_live_weight") or 0))) if approved else 0.0
    return {"execution_eligible": bool(weight > 0), "weight": weight,
            "reason": str(item.get("reason") or ("approved" if weight else "promotion_not_approved")),
            "strategy_key": strategy_key, "methodology_version": item.get("methodology_version"),
            "status": item.get("status"), "approved_by": item.get("approved_by"),
            "approved_at": item.get("approved_at"), "as_of_date": str(as_of_date),
            "evidence": item.get("evidence") or {}}


def strategy_promotion_catalog(connection: Any, as_of_date: date) -> list[dict[str, Any]]:
    """Return the promotion status for every declared strategy contract.

    A strategy present in ``STRATEGY_CONTRACTS`` but missing its registry row
    still returns a fail-closed entry rather than being silently omitted.
    """
    return [
        strategy_live_promotion(connection, key, as_of_date)
        for key in sorted(STRATEGY_CONTRACTS)
    ]


def sync_strategy_promotion_catalog(database: Any, as_of_date: date) -> list[dict[str, Any]]:
    """Open the connection itself; kept out of any ``async def`` route body.

    Repository-boundary policy requires async routes to reach sync database
    work through ``run_database_blocking`` rather than opening a transaction
    inline, so this plain (non-async) wrapper is the only place that does.
    """
    with database.transaction() as connection:
        return strategy_promotion_catalog(connection, as_of_date)


__all__ = [
    "MAX_APPROVED_WEIGHT", "strategy_live_promotion", "strategy_promotion_catalog",
    "sync_strategy_promotion_catalog",
]
