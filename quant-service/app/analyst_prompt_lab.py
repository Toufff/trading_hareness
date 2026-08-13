"""Deterministic Prompt-Lab and point-in-time analyst outcome facts.

This module intentionally does not call a model.  It makes proposed extraction
contracts, reviewer labels, and their offline measurements durable first; a
future LLM adapter can only become a challenger by writing the same contracts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from psycopg.types.json import Json


PROMPT_VARIANTS = {
    "strict_action": "strict-action-v1",
    "scenario_context": "scenario-context-v1",
    "risk_first": "risk-first-v1",
}
INTRADAY_METHODOLOGY_VERSION = "received-at-local-quote-v1"
INTRADAY_HORIZONS = (5, 15, 30, 60)


def _candidate_payload(row: dict[str, Any], variant_key: str) -> dict[str, Any]:
    """A common schema, with transparent deterministic variant constraints."""
    base = {
        "source": {"kind": row["source_kind"], "id": row["source_id"], "version": row["source_version"],
                   "content_hash": row["content_hash"], "received_at": row["received_at"].isoformat()},
        "observation": {key: row.get(key) for key in (
            "scope", "subject_key", "subject_label", "action", "direction", "horizon_days", "strength",
            "confidence", "conditions", "evidence_span", "status")},
        "contract": {"schema": "analyst-observation-v1", "variant": variant_key,
                     "strategy_effect": "none", "requires_human_gold_label": True},
    }
    if variant_key == "strict_action":
        base["contract"]["acceptance"] = "stock scope; explicit non-neutral action; mapped symbol"
        base["candidate"] = bool(row["scope"] == "stock" and int(row["direction"] or 0) != 0
                                 and str(row["subject_key"]).endswith((".SH", ".SZ", ".BJ")))
    elif variant_key == "risk_first":
        base["contract"]["acceptance"] = "negative direction or explicit risk/avoid action"
        base["candidate"] = bool(int(row["direction"] or 0) < 0 or row["action"] in {"avoid", "reduce", "sell"})
    else:
        base["contract"]["acceptance"] = "all mapped observations retained as contextual hypotheses"
        base["candidate"] = bool(row["subject_key"] and not str(row["subject_key"]).startswith("unmapped:"))
    return base


def materialize_prompt_candidates(connection: Any, *, cutoff_at: datetime | None = None) -> dict[str, Any]:
    cutoff_at = cutoff_at or datetime.now(timezone.utc)
    rows = connection.execute(
        """SELECT observation_id,analyst_id,source_kind,source_id,source_version,content_hash,received_at,
                  scope,subject_key,subject_label,action,direction,horizon_days,strength,confidence,
                  conditions,evidence_span,status
             FROM quant.analyst_observations
            WHERE strategy_available_at<=%s AND status <> 'rejected'
            ORDER BY strategy_available_at,observation_id""", (cutoff_at,)
    ).fetchall()
    inserted = 0
    for source_row in rows:
        row = dict(source_row)
        for variant_key, variant_version in PROMPT_VARIANTS.items():
            payload = _candidate_payload(row, variant_key)
            digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
            saved = connection.execute(
                """INSERT INTO quant.analyst_prompt_candidates(
                     observation_id,analyst_id,variant_key,variant_version,candidate_hash,payload)
                   VALUES(%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(observation_id,variant_key,variant_version) DO NOTHING
                   RETURNING candidate_id""",
                (row["observation_id"], row["analyst_id"], variant_key, variant_version, digest, Json(payload)),
            ).fetchone()
            inserted += int(saved is not None)
    return {"cutoff_at": cutoff_at, "source_observations": len(rows), "inserted": inserted,
            "variants": dict(PROMPT_VARIANTS), "live_effect": "none"}


def label_prompt_candidate(connection: Any, *, candidate_id: Any, label: str, direction_correct: bool | None,
                           action_executable: bool | None, reviewer: str, notes: str = "") -> dict[str, Any]:
    row = connection.execute(
        """INSERT INTO quant.analyst_prompt_gold_labels(candidate_id,label,direction_correct,action_executable,reviewer,notes)
           VALUES(%s,%s,%s,%s,%s,%s)
           ON CONFLICT(candidate_id) DO UPDATE SET label=EXCLUDED.label,direction_correct=EXCLUDED.direction_correct,
             action_executable=EXCLUDED.action_executable,reviewer=EXCLUDED.reviewer,notes=EXCLUDED.notes,labelled_at=now()
           RETURNING candidate_id,label,direction_correct,action_executable,reviewer,notes,labelled_at""",
        (candidate_id, label, direction_correct, action_executable, reviewer, notes),
    ).fetchone()
    connection.execute("UPDATE quant.analyst_prompt_candidates SET status='labelled' WHERE candidate_id=%s", (candidate_id,))
    return dict(row)


def evaluate_prompt_variant(connection: Any, *, variant_key: str, cutoff_at: datetime | None = None,
                            minimum_labels: int = 30) -> dict[str, Any]:
    if variant_key not in PROMPT_VARIANTS:
        raise ValueError("unknown prompt variant")
    cutoff_at = cutoff_at or datetime.now(timezone.utc)
    rows = connection.execute(
        """SELECT c.candidate_id,l.label,l.direction_correct,l.action_executable
             FROM quant.analyst_prompt_candidates c
             LEFT JOIN quant.analyst_prompt_gold_labels l USING(candidate_id)
            WHERE c.variant_key=%s AND c.variant_version=%s AND c.created_at<=%s""",
        (variant_key, PROMPT_VARIANTS[variant_key], cutoff_at),
    ).fetchall()
    labelled = [dict(row) for row in rows if row["label"] is not None]
    supported = sum(item["label"] == "supported" for item in labelled)
    executable = sum(bool(item["action_executable"]) for item in labelled)
    directional = [item["direction_correct"] for item in labelled if item["direction_correct"] is not None]
    status = "completed" if len(labelled) >= minimum_labels else ("insufficient_labels" if labelled else "collecting")
    metrics = {
        "labelled": len(labelled), "candidate_total": len(rows),
        "support_precision": supported / len(labelled) if labelled else None,
        "executable_precision": executable / len(labelled) if labelled else None,
        "direction_accuracy": sum(bool(value) for value in directional) / len(directional) if directional else None,
        "minimum_labels": minimum_labels, "methodology": "human-gold-only-v1",
    }
    run = connection.execute(
        """INSERT INTO quant.analyst_prompt_evaluation_runs(variant_key,variant_version,cutoff_at,status,sample_count,metrics)
           VALUES(%s,%s,%s,%s,%s,%s)
           ON CONFLICT(variant_key,variant_version,cutoff_at) DO UPDATE SET status=EXCLUDED.status,
             sample_count=EXCLUDED.sample_count,metrics=EXCLUDED.metrics
           RETURNING evaluation_id""",
        (variant_key, PROMPT_VARIANTS[variant_key], cutoff_at, status, len(labelled), Json(metrics)),
    ).fetchone()
    if status == "completed":
        connection.execute("UPDATE quant.analyst_prompt_candidates SET status='evaluated' WHERE variant_key=%s AND variant_version=%s", (variant_key, PROMPT_VARIANTS[variant_key]))
    return {"evaluation_id": str(run["evaluation_id"]), "variant_key": variant_key, "status": status,
            "metrics": metrics, "live_effect": "none; human approval and out-of-sample gates required"}


def materialize_intraday_analyst_outcomes(connection: Any, *, cutoff_at: datetime | None = None,
                                          limit: int = 2000) -> dict[str, Any]:
    """Settle only from already persisted local quotes, never fetch a new quote."""
    cutoff_at = cutoff_at or datetime.now(timezone.utc)
    rows = connection.execute(
        """SELECT observation_id,subject_key,direction,strategy_available_at
             FROM quant.analyst_observations
            WHERE scope='stock' AND direction<>0 AND status IN ('eligible','replay_only')
              AND strategy_available_at<=%s
            ORDER BY strategy_available_at LIMIT %s""", (cutoff_at, max(1, min(limit, 5000))),
    ).fetchall()
    counts = {"matured": 0, "pending": 0, "unavailable": 0}
    for raw_row in rows:
        row = dict(raw_row)
        entry = connection.execute(
            """SELECT observed_at,price,source_name FROM quant.intraday_quote_observations
                 WHERE symbol=%s AND observed_at>= %s AND observed_at<=%s AND price>0
                 ORDER BY observed_at LIMIT 1""", (row["subject_key"], row["strategy_available_at"], cutoff_at),
        ).fetchone()
        for horizon in INTRADAY_HORIZONS:
            target = row["strategy_available_at"] + timedelta(minutes=horizon)
            status, exit_row = "pending", None
            if entry is None:
                status = "unavailable" if cutoff_at >= target else "pending"
            elif cutoff_at >= target:
                exit_row = connection.execute(
                    """SELECT observed_at,price,source_name FROM quant.intraday_quote_observations
                         WHERE symbol=%s AND observed_at>= %s AND observed_at<=%s AND price>0
                         ORDER BY observed_at DESC LIMIT 1""", (row["subject_key"], target, cutoff_at),
                ).fetchone()
                status = "matured" if exit_row is not None else "unavailable"
            directional_return = None
            if status == "matured" and entry is not None and exit_row is not None:
                directional_return = (Decimal(str(exit_row["price"])) / Decimal(str(entry["price"])) - 1) * int(row["direction"])
            connection.execute(
                """INSERT INTO quant.analyst_intraday_outcomes(
                     observation_id,methodology_version,horizon_minutes,status,entry_at,entry_price,exit_at,exit_price,
                     directional_return,source_name,calculated_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                   ON CONFLICT(observation_id,methodology_version,horizon_minutes) DO UPDATE SET status=EXCLUDED.status,
                     entry_at=EXCLUDED.entry_at,entry_price=EXCLUDED.entry_price,exit_at=EXCLUDED.exit_at,
                     exit_price=EXCLUDED.exit_price,directional_return=EXCLUDED.directional_return,
                     source_name=EXCLUDED.source_name,calculated_at=now()""",
                (row["observation_id"], INTRADAY_METHODOLOGY_VERSION, horizon, status,
                 entry["observed_at"] if entry else None, entry["price"] if entry else None,
                 exit_row["observed_at"] if exit_row else None, exit_row["price"] if exit_row else None,
                 directional_return, (exit_row or entry or {}).get("source_name")),
            )
            counts[status] += 1
    return {"observations": len(rows), "outcomes": counts, "cutoff_at": cutoff_at,
            "methodology_version": INTRADAY_METHODOLOGY_VERSION, "live_effect": "none"}


__all__ = ["INTRADAY_HORIZONS", "INTRADAY_METHODOLOGY_VERSION", "PROMPT_VARIANTS",
           "evaluate_prompt_variant", "label_prompt_candidate", "materialize_intraday_analyst_outcomes",
           "materialize_prompt_candidates"]
