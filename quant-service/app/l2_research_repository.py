"""Persistence boundary for the Level-2 incremental-value research gate."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.types.json import Json


def persist_l2_evaluation(
    database: Any,
    *,
    source_kind: str,
    algorithm_version: str,
    minimum_samples: int,
    evaluation: dict[str, Any],
    evidence_window_start: datetime | None = None,
    evidence_window_end: datetime | None = None,
) -> dict[str, Any]:
    """Store a bounded gate result and provenance, never provider payloads."""
    evidence = {
        "source_kind": source_kind,
        "evidence_window_start": evidence_window_start,
        "evidence_window_end": evidence_window_end,
        "notice": "Research evidence only; no live threshold or order path is changed.",
    }
    with database.transaction() as connection:
        row = connection.execute(
            """INSERT INTO quant.l2_incremental_value_evaluations(
                   source_kind,algorithm_version,minimum_samples,samples,
                   mean_incremental_value,ci95_lower,status,l2_algorithm_versions,
                   evidence,live_effect
               ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'none')
               RETURNING evaluation_id,evaluated_at""",
            (
                source_kind, algorithm_version, int(minimum_samples), int(evaluation.get("samples") or 0),
                evaluation.get("mean_incremental_value"), evaluation.get("ci95_lower"),
                str(evaluation.get("status") or "blocked"), Json(evaluation.get("l2_algorithm_versions") or []),
                Json(evidence),
            ),
        ).fetchone()
    return {**evaluation, "evaluation_id": str(row["evaluation_id"]), "evaluated_at": row["evaluated_at"],
            "source_kind": source_kind, "algorithm_version": algorithm_version,
            "evidence_window_start": evidence_window_start, "evidence_window_end": evidence_window_end,
            "research_only": True, "promotion_record_required": True}


async def latest_l2_evaluation(async_database: Any) -> dict[str, Any]:
    """Return the last persisted gate result, or an explicit blocked state."""
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT evaluation_id,evaluated_at,source_kind,algorithm_version,minimum_samples,
                      samples,mean_incremental_value,ci95_lower,status,l2_algorithm_versions,
                      evidence,live_effect
                 FROM quant.l2_incremental_value_evaluations
                ORDER BY evaluated_at DESC LIMIT 1"""
        )
        row = await result.fetchone()
    if not row:
        return {
            "status": "blocked", "reason": "no_persisted_licensed_level2_evaluation",
            "samples": 0, "live_effect": "none", "research_only": True,
            "promotion_record_required": True,
        }
    return {**dict(row), "evaluation_id": str(row["evaluation_id"]), "research_only": True,
            "promotion_record_required": True}


__all__ = ["persist_l2_evaluation", "latest_l2_evaluation"]
