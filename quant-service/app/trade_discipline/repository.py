"""Append-only persistence and readback for discipline plans and their history.

Every write is an insert.  Idempotency is by ``content_hash`` under a natural
key: re-running the same generation returns the stored row, while the same key
carrying different content is a conflict the caller must resolve by writing a
*new* plan that supersedes the old one.  That is the same rule
``personal_decision_repository`` uses, and ``DisciplineFactConflict`` subclasses
its error so an existing HTTP boundary keeps mapping it to one status.

No business logic lives here: the plan is already derived and quality-checked
before it arrives, and a plan that failed the gate is stored exactly like one
that passed.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from psycopg.types.json import Json

from ..personal_decision_repository import ImmutableDecisionFactConflict
from .contracts import ComplianceRecord, DisciplinePlan, Evaluation, Review

PLAN_COLUMNS = """plan_id,run_id,plan_key,contract_version,account_key,symbol,name,plan_kind,stage,
                  template_key,template_version,as_of_at,trading_date,valid_until,position,metrics,
                  sizing,lines,evidence_refs,quality,status,supersedes_plan_id,lowered_reason,
                  inputs_hash,generator_version,content_hash,created_at"""
ACTIVE_STATUSES = ("active", "rejected_by_quality")


class DisciplineFactConflict(ImmutableDecisionFactConflict):
    """The caller reused a natural key for different immutable content."""


def content_hash(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _one(connection: Any, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = connection.execute(sql, params).fetchone()
    return dict(row) if row else None


def _rows(connection: Any, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


# --------------------------------------------------------------------------
# Generation runs
# --------------------------------------------------------------------------
def persist_generation_run(connection: Any, *, run_id: str, account_key: str, as_of_at: Any, trading_date: Any,
                           generator_version: str, inputs_hash: str, inputs: dict[str, Any],
                           status: str = "generated") -> dict[str, Any]:
    """Record the frozen evidence one or more plans were derived from."""
    existing = _one(connection, "SELECT run_id,inputs_hash FROM quant.discipline_generation_runs WHERE run_id=%s",
                    (run_id,))
    if existing:
        if str(existing["inputs_hash"]) != inputs_hash:
            raise DisciplineFactConflict("run_id already exists with a different inputs_hash")
        return {"status": "idempotent", "run_id": str(existing["run_id"]), "inputs_hash": inputs_hash}
    row = _one(connection, """
        INSERT INTO quant.discipline_generation_runs(
            run_id,account_key,as_of_at,trading_date,generator_version,inputs_hash,inputs,status)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING run_id,created_at""",
               (run_id, account_key, as_of_at, trading_date, generator_version, inputs_hash, Json(inputs), status))
    return {"status": "created", "run_id": str(row["run_id"]), "inputs_hash": inputs_hash,
            "created_at": row["created_at"]}


def read_generation_run(connection: Any, run_id: str) -> dict[str, Any] | None:
    return _one(connection, """
        SELECT run_id,account_key,as_of_at,trading_date,generator_version,inputs_hash,inputs,status,created_at
          FROM quant.discipline_generation_runs WHERE run_id=%s""", (run_id,))


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------
def persist_plan(connection: Any, plan: DisciplinePlan, *, run_id: str | None = None) -> dict[str, Any]:
    """Insert one plan and read it back; identical content under the same key is idempotent."""
    payload = plan.model_dump(mode="json")
    digest = content_hash(payload)
    existing = _one(connection, "SELECT plan_id,content_hash FROM quant.discipline_plans WHERE plan_key=%s",
                    (plan.plan_key,))
    if existing:
        if str(existing["content_hash"]) != digest:
            raise DisciplineFactConflict("plan_key already exists with different content")
        return {"status": "idempotent", "plan_id": str(existing["plan_id"]), "content_hash": digest,
                "plan": read_plan(connection, str(existing["plan_id"]))}
    connection.execute(
        """INSERT INTO quant.instruments(symbol,exchange,name,source)
           VALUES(%s,%s,%s,'trade_discipline')
           ON CONFLICT(symbol) DO UPDATE SET name=COALESCE(NULLIF(EXCLUDED.name,''),quant.instruments.name)""",
        (plan.symbol, plan.symbol.rsplit(".", 1)[-1], plan.name),
    )
    row = _one(connection, """
        INSERT INTO quant.discipline_plans(
            run_id,plan_key,contract_version,account_key,symbol,name,plan_kind,stage,template_key,
            template_version,as_of_at,trading_date,valid_until,position,metrics,sizing,lines,
            evidence_refs,quality,status,supersedes_plan_id,lowered_reason,inputs_hash,generator_version,content_hash)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING plan_id,created_at""",
               (run_id or plan.run_id, plan.plan_key, plan.contract_version, plan.account_key, plan.symbol,
                plan.name, plan.plan_kind, plan.stage, plan.template_key, plan.template_version,
                plan.as_of_at, plan.trading_date, plan.valid_until,
                Json(payload["position"]) if payload.get("position") is not None else None,
                Json(payload["metrics"]), Json(payload["sizing"]) if payload.get("sizing") is not None else None,
                Json(payload["lines"]), Json(payload["evidence_refs"]), Json(payload["quality"]),
                plan.status, plan.supersedes_plan_id, plan.lowered_reason, plan.inputs_hash,
                plan.generator_version, digest))
    plan_id = str(row["plan_id"])
    return {"status": "created", "plan_id": plan_id, "content_hash": digest,
            "created_at": row["created_at"], "plan": read_plan(connection, plan_id)}


def plan_from_row(row: Any) -> DisciplinePlan:
    """Rebuild the frozen contract object from one stored row.

    Evaluation and reconciliation run against the plan exactly as it was
    persisted, never against a freshly derived one, so a stored row is the only
    thing a later judgement is allowed to read.
    """
    payload = {key: row.get(key) for key in DisciplinePlan.model_fields}
    payload["run_id"] = str(row.get("run_id") or row.get("plan_key"))
    supersedes = row.get("supersedes_plan_id")
    payload["supersedes_plan_id"] = str(supersedes) if supersedes else None
    return DisciplinePlan(**payload)


def read_plan(connection: Any, plan_id: str) -> dict[str, Any] | None:
    return _one(connection, f"SELECT {PLAN_COLUMNS} FROM quant.discipline_plans WHERE plan_id=%s", (plan_id,))


def read_plan_by_key(connection: Any, plan_key: str) -> dict[str, Any] | None:
    return _one(connection, f"SELECT {PLAN_COLUMNS} FROM quant.discipline_plans WHERE plan_key=%s", (plan_key,))


def latest_plans(connection: Any, account_key: str, *, statuses: tuple[str, ...] = ACTIVE_STATUSES,
                 limit: int = 50) -> list[dict[str, Any]]:
    """The newest plan per symbol for one account, newest symbol first."""
    return _rows(connection, f"""
        SELECT * FROM (
          SELECT DISTINCT ON (symbol) {PLAN_COLUMNS}
            FROM quant.discipline_plans
           WHERE account_key=%s AND status = ANY(%s)
           ORDER BY symbol,as_of_at DESC) latest
        ORDER BY as_of_at DESC,symbol LIMIT %s""", (account_key, list(statuses), limit))


def mark_superseded(connection: Any, plan_id: str, *, by_plan_id: str) -> bool:
    """Flip one active plan's lifecycle flag; content is never edited in place."""
    result = connection.execute(
        """UPDATE quant.discipline_plans SET status='superseded'
            WHERE plan_id=%s AND status='active' AND plan_id<>%s""", (plan_id, by_plan_id))
    return bool(getattr(result, "rowcount", 0) or 0)


# --------------------------------------------------------------------------
# Evaluations / compliance / reviews
# --------------------------------------------------------------------------
def persist_evaluation(connection: Any, evaluation: Evaluation) -> dict[str, Any]:
    """One evaluation per (plan, moment, basis); re-evaluating the same moment is idempotent."""
    payload = evaluation.model_dump(mode="json")
    digest = content_hash(payload)
    existing = _one(connection, """
        SELECT evaluation_id,content_hash FROM quant.discipline_evaluations
         WHERE plan_id=%s AND as_of_at=%s AND basis=%s""",
                    (evaluation.plan_id, evaluation.as_of_at, evaluation.basis))
    if existing:
        if str(existing["content_hash"]) != digest:
            raise DisciplineFactConflict("evaluation already exists for this plan/moment with different content")
        return {"status": "idempotent", "evaluation_id": str(existing["evaluation_id"]), "content_hash": digest,
                "evaluation": read_evaluation(connection, str(existing["evaluation_id"]))}
    row = _one(connection, """
        INSERT INTO quant.discipline_evaluations(
            plan_id,as_of_at,trading_date,basis,line_states,plan_state,inputs_hash,content_hash)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING evaluation_id,created_at""",
               (evaluation.plan_id, evaluation.as_of_at, evaluation.trading_date, evaluation.basis,
                Json(payload["line_states"]), evaluation.plan_state, evaluation.inputs_hash, digest))
    evaluation_id = str(row["evaluation_id"])
    return {"status": "created", "evaluation_id": evaluation_id, "content_hash": digest,
            "created_at": row["created_at"], "evaluation": read_evaluation(connection, evaluation_id)}


def read_evaluation(connection: Any, evaluation_id: str) -> dict[str, Any] | None:
    return _one(connection, """
        SELECT evaluation_id,plan_id,as_of_at,trading_date,basis,line_states,plan_state,inputs_hash,
               content_hash,created_at
          FROM quant.discipline_evaluations WHERE evaluation_id=%s""", (evaluation_id,))


def latest_evaluation(connection: Any, plan_id: str, *, basis: str | None = None) -> dict[str, Any] | None:
    return _one(connection, """
        SELECT evaluation_id,plan_id,as_of_at,trading_date,basis,line_states,plan_state,inputs_hash,
               content_hash,created_at
          FROM quant.discipline_evaluations
         WHERE plan_id=%s AND (%s::text IS NULL OR basis=%s)
         ORDER BY as_of_at DESC LIMIT 1""", (plan_id, basis, basis))


def plan_evaluations(connection: Any, plan_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """Every evaluation of one plan, oldest first - the series reconciliation reads."""
    return _rows(connection, """
        SELECT evaluation_id,plan_id,as_of_at,trading_date,basis,line_states,plan_state,inputs_hash,
               content_hash,created_at
          FROM quant.discipline_evaluations WHERE plan_id=%s ORDER BY as_of_at,created_at LIMIT %s""",
                 (plan_id, limit))


def persist_compliance(connection: Any, record: ComplianceRecord) -> dict[str, Any]:
    """Append one reconciliation verdict; the identical verdict twice is idempotent."""
    payload = record.model_dump(mode="json")
    digest = content_hash(payload)
    existing = _one(connection, """
        SELECT compliance_id FROM quant.discipline_compliance WHERE plan_id=%s AND content_hash=%s""",
                    (record.plan_id, digest))
    if existing:
        return {"status": "idempotent", "compliance_id": str(existing["compliance_id"]), "content_hash": digest,
                "compliance": read_compliance(connection, str(existing["compliance_id"]))}
    row = _one(connection, """
        INSERT INTO quant.discipline_compliance(
            plan_id,trade_record_id,line_kind,verdict,deviation,notes,content_hash)
        VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING compliance_id,created_at""",
               (record.plan_id, record.trade_record_id, record.line_kind, record.verdict,
                Json(payload["deviation"]), record.notes, digest))
    compliance_id = str(row["compliance_id"])
    return {"status": "created", "compliance_id": compliance_id, "content_hash": digest,
            "created_at": row["created_at"], "compliance": read_compliance(connection, compliance_id)}


def read_compliance(connection: Any, compliance_id: str) -> dict[str, Any] | None:
    return _one(connection, """
        SELECT compliance_id,plan_id,trade_record_id,line_kind,verdict,deviation,notes,content_hash,created_at
          FROM quant.discipline_compliance WHERE compliance_id=%s""", (compliance_id,))


def plan_compliance(connection: Any, plan_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    return _rows(connection, """
        SELECT compliance_id,plan_id,trade_record_id,line_kind,verdict,deviation,notes,content_hash,created_at
          FROM quant.discipline_compliance WHERE plan_id=%s ORDER BY created_at DESC LIMIT %s""", (plan_id, limit))


def persist_review(connection: Any, review: Review) -> dict[str, Any]:
    """Append one human verdict on a plan; the review itself is immutable."""
    payload = review.model_dump(mode="json")
    digest = content_hash(payload)
    existing = _one(connection, """
        SELECT review_id FROM quant.discipline_reviews WHERE plan_id=%s AND content_hash=%s""",
                    (review.plan_id, digest))
    if existing:
        return {"status": "idempotent", "review_id": str(existing["review_id"]), "content_hash": digest,
                "review": read_review(connection, str(existing["review_id"]))}
    row = _one(connection, """
        INSERT INTO quant.discipline_reviews(plan_id,reviewer,verdict,notes,overrides,content_hash)
        VALUES(%s,%s,%s,%s,%s,%s) RETURNING review_id,created_at""",
               (review.plan_id, review.reviewer, review.verdict, review.notes, Json(payload["overrides"]), digest))
    review_id = str(row["review_id"])
    return {"status": "created", "review_id": review_id, "content_hash": digest,
            "created_at": row["created_at"], "review": read_review(connection, review_id)}


def read_review(connection: Any, review_id: str) -> dict[str, Any] | None:
    return _one(connection, """
        SELECT review_id,plan_id,reviewer,verdict,notes,overrides,content_hash,created_at
          FROM quant.discipline_reviews WHERE review_id=%s""", (review_id,))


def plan_reviews(connection: Any, plan_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    return _rows(connection, """
        SELECT review_id,plan_id,reviewer,verdict,notes,overrides,content_hash,created_at
          FROM quant.discipline_reviews WHERE plan_id=%s ORDER BY created_at DESC LIMIT %s""", (plan_id, limit))


__all__ = [
    "ACTIVE_STATUSES", "DisciplineFactConflict", "content_hash", "latest_evaluation", "latest_plans",
    "mark_superseded", "persist_compliance", "persist_evaluation", "persist_generation_run", "persist_plan",
    "persist_review", "plan_compliance", "plan_evaluations", "plan_from_row", "plan_reviews",
    "read_compliance", "read_evaluation",
    "read_generation_run", "read_plan", "read_plan_by_key", "read_review",
]
