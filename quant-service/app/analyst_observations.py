"""Unified append-only analyst observation materialization.

The observation table is an auditable extraction fact, not a live weight.  A
separate promotion registry remains the only authority for analyst influence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Json


EXTRACTION_SCHEMA_VERSION = "analyst-observation-v1"
OBSERVATION_EXTRACTOR_VERSION = "deterministic-claim-adapter-v1"


def observation_action(direction: int, raw: dict[str, Any] | None = None) -> str:
    raw = raw if isinstance(raw, dict) else {}
    source = str(raw.get("direction_source") or "")
    if "negative" in source or direction < 0:
        return "reduce" if any(token in source for token in ("减", "止损", "风险")) else "avoid"
    if "positive" in source or direction > 0:
        return "buy" if any(token in source for token in ("买", "加仓", "开仓")) else "watch"
    return "neutral"


def observation_status(*, scope: str, subject_key: str, direction: int, confidence: float | None,
                       source_kind: str, stated_at: datetime | None, available_at: datetime) -> str:
    if not subject_key or subject_key.startswith("unmapped:"):
        return "unmapped"
    if direction == 0:
        return "neutral"
    # Author time is not the strategy clock. A delayed/author-timed claim is
    # retained for replay, while local receipt remains the immutable PIT time.
    if source_kind == "message" and stated_at is not None and abs((available_at - stated_at).total_seconds()) > 300:
        return "replay_only"
    if confidence is None or confidence < 0.70:
        return "replay_only"
    return "eligible"


def persist_extraction_run(connection: Any, *, analyst_id: str, source_kind: str, source_id: str,
                           source_version: str, content_hash: str, status: str = "completed",
                           candidate_count: int = 0, accepted_count: int = 0,
                           uncertainty: dict[str, Any] | None = None) -> Any:
    row = connection.execute(
        """INSERT INTO quant.analyst_extraction_runs(
             analyst_id,source_kind,source_id,source_version,content_hash,extractor_version,schema_version,
             status,candidate_count,accepted_count,uncertainty,finished_at)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
           RETURNING extraction_run_id""",
        (analyst_id, source_kind, source_id, source_version, content_hash,
         OBSERVATION_EXTRACTOR_VERSION, EXTRACTION_SCHEMA_VERSION, status,
         int(candidate_count), int(accepted_count), Json(uncertainty or {})),
    ).fetchone()
    return row["extraction_run_id"]


def persist_observations_for_evidence(connection: Any, *, evidence_id: Any, extraction_run_id: Any,
                                      analyst_id: str, source_kind: str, source_id: str,
                                      source_version: str, content_hash: str, received_at: datetime,
                                      strategy_available_at: datetime, published_at: datetime | None,
                                      edited_at: datetime | None, stated_at: datetime | None,
                                      stated_precision: str | None) -> int:
    claims = connection.execute(
        """SELECT c.scope,c.subject_key,c.subject_label,c.direction,c.strength,c.horizon_days,
                  c.extraction_confidence,c.raw,e.body
             FROM quant.analyst_claims c JOIN quant.analyst_evidence e ON e.evidence_id=c.evidence_id
            WHERE c.evidence_id=%s ORDER BY c.claim_id""", (evidence_id,),
    ).fetchall()
    stored = 0
    for claim in claims:
        raw = dict(claim["raw"] or {})
        scope = str(claim["scope"])
        subject_key = str(claim["subject_key"] or "")
        confidence = float(claim["extraction_confidence"]) if claim["extraction_confidence"] is not None else None
        direction = int(claim["direction"] or 0)
        action = observation_action(direction, raw)
        status = observation_status(scope=scope, subject_key=subject_key, direction=direction,
                                    confidence=confidence, source_kind=source_kind,
                                    stated_at=stated_at, available_at=strategy_available_at)
        row = connection.execute(
            """INSERT INTO quant.analyst_observations(
                 extraction_run_id,analyst_id,source_kind,source_id,source_version,content_hash,
                 received_at,strategy_available_at,published_at,edited_at,stated_at,stated_precision,
                 scope,subject_key,subject_label,action,direction,horizon_days,strength,confidence,
                 conditions,evidence_span,extractor_version,status)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(source_kind,source_id,source_version,content_hash,scope,subject_key,horizon_days,extractor_version)
               DO NOTHING RETURNING observation_id""",
            (extraction_run_id, analyst_id, source_kind, source_id, source_version, content_hash,
             received_at, strategy_available_at, published_at, edited_at, stated_at, stated_precision,
             scope, subject_key, str(claim["subject_label"] or ""), action, direction,
             claim["horizon_days"], claim["strength"], claim["extraction_confidence"],
             Json({"source": "analyst_claim", "direction_source": raw.get("direction_source"),
                   "position_intent": raw.get("position_intent")}), str(claim["body"] or ""),
             OBSERVATION_EXTRACTOR_VERSION, status),
        ).fetchone()
        stored += int(row is not None)
    return stored


__all__ = ["EXTRACTION_SCHEMA_VERSION", "OBSERVATION_EXTRACTOR_VERSION",
           "observation_action", "observation_status", "persist_extraction_run",
           "persist_observations_for_evidence"]
