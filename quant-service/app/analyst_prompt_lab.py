"""Deterministic Prompt-Lab and point-in-time analyst outcome facts.

This module intentionally does not call a model.  It makes proposed extraction
contracts, reviewer labels, and their offline measurements durable first; a
future LLM adapter can only become a challenger by writing the same contracts.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from .analyst_intraday_settlement import (
    EXIT_QUOTE_TOLERANCE_SECONDS,
    entry_quote_window,
    has_bounded_query_window,
    json_safe_window,
)
from .intraday_clock import intraday_outcome_window


PROMPT_VARIANTS = {
    "strict_action": "strict-action-v1",
    "scenario_context": "scenario-context-v1",
    "risk_first": "risk-first-v1",
}
# Never overwrite the original v1 ledger: it was not bounded to a single
# continuous-auction segment, so it could incorrectly borrow an afternoon or
# next-day quote.  Keeping this version separate makes the correction
# reproducible and leaves the old research artifact auditable.
INTRADAY_METHODOLOGY_VERSION = "received-at-local-quote-session-bounded-v2"
INTRADAY_HORIZONS = (5, 15, 30, 60)


def _json_compatible(value: Any) -> Any:
    """Normalize database-native values before hashing or storing JSON facts."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    return value


def intraday_path_metrics(entry_price: Any, direction: int, minimum_price: Any, maximum_price: Any,
                          quote_count: Any) -> dict[str, Any] | None:
    """Return directional MFE/MAE over a bounded same-session quote path.

    ``mfe`` is the best unrealised directional return and ``mae`` the worst.
    A path without both extrema is deliberately unavailable rather than
    substituted with a later/lunch/overnight quote.
    """
    try:
        entry = Decimal(str(entry_price))
        minimum = Decimal(str(minimum_price))
        maximum = Decimal(str(maximum_price))
        count = int(quote_count or 0)
    except Exception:  # noqa: BLE001 - values are third-party persisted evidence
        return None
    if entry <= 0 or minimum <= 0 or maximum <= 0 or count <= 0 or direction not in {-1, 1}:
        return None
    if direction > 0:
        mfe = maximum / entry - 1
        mae = minimum / entry - 1
    else:
        mfe = entry / minimum - 1
        mae = entry / maximum - 1
    return {"mfe": float(mfe), "mae": float(mae), "path_quote_count": count,
            "minimum_price": float(minimum), "maximum_price": float(maximum)}


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
    return _json_compatible(base)


def _label_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    supported = sum(item["label"] == "supported" for item in rows)
    executable = sum(bool(item["action_executable"]) for item in rows)
    directional = [item["direction_correct"] for item in rows if item["direction_correct"] is not None]
    return {
        "labelled": len(rows),
        "support_precision": supported / len(rows) if rows else None,
        "executable_precision": executable / len(rows) if rows else None,
        "direction_accuracy": sum(bool(value) for value in directional) / len(directional) if directional else None,
    }


def _chronological_label_split(rows: list[dict[str, Any]], *, holdout_fraction: float = 0.20) -> dict[str, Any]:
    """Split human labels by point-in-time observation day, never at random."""
    dated: list[tuple[dict[str, Any], Any]] = []
    for row in rows:
        available_at = row.get("strategy_available_at")
        if isinstance(available_at, datetime):
            dated.append((row, available_at.astimezone(ZoneInfo("Asia/Shanghai")).date()))
    dates = sorted({day for _, day in dated})
    if not dates:
        return {"training": [], "holdout": [], "training_days": [], "holdout_days": []}
    holdout_days = max(1, math.ceil(len(dates) * holdout_fraction))
    holdout_day_set = set(dates[-holdout_days:])
    return {
        "training": [row for row, day in dated if day not in holdout_day_set],
        "holdout": [row for row, day in dated if day in holdout_day_set],
        "training_days": [str(day) for day in dates if day not in holdout_day_set],
        "holdout_days": [str(day) for day in dates if day in holdout_day_set],
    }


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
        """SELECT c.candidate_id,l.label,l.direction_correct,l.action_executable,o.strategy_available_at
             FROM quant.analyst_prompt_candidates c
             JOIN quant.analyst_observations o USING(observation_id)
             LEFT JOIN quant.analyst_prompt_gold_labels l USING(candidate_id)
            WHERE c.variant_key=%s AND c.variant_version=%s AND o.strategy_available_at<=%s
              AND (l.labelled_at IS NULL OR l.labelled_at<=%s)""",
        (variant_key, PROMPT_VARIANTS[variant_key], cutoff_at, cutoff_at),
    ).fetchall()
    labelled = [dict(row) for row in rows if row["label"] is not None]
    split = _chronological_label_split(labelled)
    minimum_holdout_labels = max(10, math.ceil(minimum_labels * 0.20))
    time_gate = bool(split["training_days"] and split["holdout_days"]
                     and len(split["holdout"]) >= minimum_holdout_labels
                     and len(split["training"]) >= minimum_labels - minimum_holdout_labels)
    status = "completed" if len(labelled) >= minimum_labels and time_gate else ("insufficient_labels" if labelled else "collecting")
    metrics = {
        "candidate_total": len(rows), "minimum_labels": minimum_labels,
        "methodology": "human-gold-chronological-holdout-v2",
        "all_labelled": _label_metrics(labelled),
        "training": _label_metrics(split["training"]),
        "holdout": _label_metrics(split["holdout"]),
        "time_split": {
            "unit": "Asia/Shanghai strategy_available_at date", "holdout_fraction": 0.20,
            "training_days": split["training_days"], "holdout_days": split["holdout_days"],
            "minimum_holdout_labels": minimum_holdout_labels, "time_gate_passed": time_gate,
        },
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
    """Settle only from already persisted, same-session Tencent quotes.

    An analyst observation is usable only from ``strategy_available_at``.  We
    accept its first local Tencent quote within a short bounded window, then
    use the *actual* entry quote time to calculate each horizon.  Neither the
    11:30--13:00 lunch interval nor an overnight quote may satisfy an exit.
    """
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
        entry_window = entry_quote_window(row["strategy_available_at"], cutoff_at=cutoff_at)
        entry = None
        # A later post-close recompute must still inspect the *historical*
        # 90-second entry window.  ``unavailable`` here means "no quote has
        # been found yet", not that the window is unsafe to read.
        if has_bounded_query_window(entry_window):
            entry = connection.execute(
                """SELECT observed_at,price,source_name FROM quant.intraday_quote_observations
                     WHERE symbol=%s AND source_name='tencent_free'
                       AND observed_at>=%s AND observed_at<=%s AND price>0
                     ORDER BY observed_at LIMIT 1""",
                (row["subject_key"], entry_window["query_start"], entry_window["query_end"]),
            ).fetchone()
        for horizon in INTRADAY_HORIZONS:
            status, exit_row = "pending", None
            settlement: dict[str, Any] = {
                "clock_basis": "strategy_available_at",
                "entry_window": json_safe_window(entry_window),
                "source": "tencent_free",
                "session_bounded": True,
            }
            if entry is None:
                status = str(entry_window["status"])
                settlement["reason"] = entry_window.get("reason")
            else:
                exit_window = intraday_outcome_window(
                    entry["observed_at"], horizon_minutes=horizon, cutoff=cutoff_at,
                    tolerance_seconds=EXIT_QUOTE_TOLERANCE_SECONDS,
                )
                settlement["entry_observed_at"] = entry["observed_at"].isoformat()
                settlement["exit_window"] = json_safe_window(exit_window)
                # As above, settlement after the tolerance expires may query
                # the persisted bounded interval, but never any later quote.
                if has_bounded_query_window(exit_window):
                    exit_row = connection.execute(
                        """SELECT observed_at,price,source_name FROM quant.intraday_quote_observations
                             WHERE symbol=%s AND source_name=%s
                               AND observed_at>=%s AND observed_at<=%s AND price>0
                             ORDER BY observed_at LIMIT 1""",
                        (row["subject_key"], entry["source_name"], exit_window["query_start"], exit_window["query_end"]),
                    ).fetchone()
                status = "matured" if exit_row is not None else str(exit_window["status"])
                settlement["reason"] = "first_quote_within_target_tolerance" if exit_row else exit_window.get("reason")
            directional_return = None
            if status == "matured" and entry is not None and exit_row is not None:
                directional_return = (Decimal(str(exit_row["price"])) / Decimal(str(entry["price"])) - 1) * int(row["direction"])
                path = connection.execute(
                    """SELECT count(*)::int path_quote_count,min(price) minimum_price,max(price) maximum_price
                         FROM quant.intraday_quote_observations
                        WHERE symbol=%s AND source_name=%s AND observed_at>=%s AND observed_at<=%s AND price>0""",
                    (row["subject_key"], entry["source_name"], entry["observed_at"], exit_row["observed_at"]),
                ).fetchone()
                metrics = intraday_path_metrics(
                    entry["price"], int(row["direction"]),
                    path.get("minimum_price") if path else None,
                    path.get("maximum_price") if path else None,
                    path.get("path_quote_count") if path else None,
                )
                if metrics is None:
                    settlement["path"] = {"status": "unavailable", "reason": "bounded_path_extrema_missing"}
                else:
                    settlement["path"] = {"status": "matured", **metrics}
            connection.execute(
                """INSERT INTO quant.analyst_intraday_outcomes(
                     observation_id,methodology_version,horizon_minutes,status,entry_at,entry_price,exit_at,exit_price,
                     directional_return,source_name,settlement,calculated_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                   ON CONFLICT(observation_id,methodology_version,horizon_minutes) DO UPDATE SET status=EXCLUDED.status,
                     entry_at=EXCLUDED.entry_at,entry_price=EXCLUDED.entry_price,exit_at=EXCLUDED.exit_at,
                     exit_price=EXCLUDED.exit_price,directional_return=EXCLUDED.directional_return,
                     source_name=EXCLUDED.source_name,settlement=EXCLUDED.settlement,calculated_at=now()""",
                (row["observation_id"], INTRADAY_METHODOLOGY_VERSION, horizon, status,
                 entry["observed_at"] if entry else None, entry["price"] if entry else None,
                 exit_row["observed_at"] if exit_row else None, exit_row["price"] if exit_row else None,
                 directional_return, (exit_row or entry or {}).get("source_name"), Json(settlement)),
            )
            counts[status] += 1
    return {"observations": len(rows), "outcomes": counts, "cutoff_at": cutoff_at,
            "methodology_version": INTRADAY_METHODOLOGY_VERSION, "live_effect": "none"}


__all__ = ["INTRADAY_HORIZONS", "INTRADAY_METHODOLOGY_VERSION", "PROMPT_VARIANTS",
           "evaluate_prompt_variant", "label_prompt_candidate", "materialize_intraday_analyst_outcomes",
           "materialize_prompt_candidates", "intraday_path_metrics"]
