"""Explicit, read-only-at-projection junction from theses to discipline plans.

Bindings are append-only facts.  This module never creates a discipline plan,
holding, position, trade, or evaluation and never mutates the referenced plan.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from ..trade_discipline.repository import read_plan
from .repository import ThesisConflict, ThesisNotFound, active_thesis, latest_binding, persist_binding


class BindingValidationError(ValueError):
    """An explicit binding does not match a persisted thesis/discipline fact."""


def _aware(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise BindingValidationError("binding timestamps must include a timezone")
    return parsed


def _has_active_successor(connection: Any, plan_id: str, cutoff: datetime) -> bool:
    return bool(connection.execute("""
        SELECT 1 FROM quant.discipline_plans
         WHERE supersedes_plan_id=%s AND status='active'
           AND as_of_at<=%s AND created_at<=%s LIMIT 1
    """, (plan_id, cutoff, cutoff)).fetchone())


def _latest_settled_session(connection: Any, cutoff: datetime) -> date | None:
    local = cutoff.astimezone(ZoneInfo("Asia/Shanghai"))
    candidate = local.date() if local.time() >= time(15, 0) else local.date() - timedelta(days=1)
    row = connection.execute("""
        SELECT max(calendar_date) AS trading_date
          FROM quant.market_trade_calendar
         WHERE is_open AND calendar_date<=%s AND available_at<=%s
    """, (candidate, cutoff)).fetchone()
    return None if not row or row.get("trading_date") is None else row["trading_date"]


def create_binding(
    connection: Any, thesis_id: str, *, thesis_revision: int, account_key: str,
    symbol: str, position_episode_id: str, plan_id: str,
    binding_source: str = "manual_api", evidence_refs: list[str] | None = None,
    bound_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Validate both stored sides, then append one explicit binding fact."""
    moment = _aware(bound_at)
    try:
        plan_id = str(UUID(str(plan_id)))
    except ValueError as error:
        raise BindingValidationError("plan_id must be a UUID") from error
    # A just-captured thesis can have a database ``created_at`` a few
    # microseconds after the caller's clock sample. Binding is a current-state
    # write, not historical replay, so validate against the current projection.
    thesis = active_thesis(connection, thesis_id)
    if thesis is None:
        raise ThesisNotFound(thesis_id)
    if int(thesis.get("revision") or 0) != thesis_revision:
        raise ThesisConflict("thesis_revision does not match the active thesis")
    if str(thesis.get("symbol")) != symbol:
        raise BindingValidationError("binding symbol does not match the thesis")

    plan = read_plan(connection, plan_id)
    if plan is None:
        raise BindingValidationError("discipline plan does not exist")
    if str(plan.get("account_key")) != account_key:
        raise BindingValidationError("discipline plan account does not match the binding")
    if str(plan.get("symbol")) != symbol:
        raise BindingValidationError("discipline plan symbol does not match the binding")
    plan_kind = str(plan.get("plan_kind"))
    if plan_kind not in {"holding", "new_buy"}:
        raise BindingValidationError("unsupported discipline plan_kind")
    if plan_kind == "holding":
        if not isinstance(plan.get("position"), dict):
            raise BindingValidationError("holding binding requires a persisted position")
        if int((plan.get("position") or {}).get("quantity") or 0) <= 0:
            raise BindingValidationError("holding plan has no positive persisted position")
        snapshot_ref = "position_snapshot:" + str(plan["position"].get("snapshot_id") or "")
        if snapshot_ref == "position_snapshot:" or snapshot_ref not in (evidence_refs or []):
            raise BindingValidationError("binding evidence_refs must anchor the persisted position snapshot")
    else:
        expected_episode = f"prospective:plan:{plan_id}"
        if position_episode_id != expected_episode or binding_source != "manual_new_buy":
            raise BindingValidationError(
                "new_buy binding requires its prospective plan episode and manual_new_buy source"
            )
    if str(plan.get("status")) != "active" or _aware(plan.get("valid_until")) <= moment:
        raise BindingValidationError("discipline plan is not currently active")
    if _aware(plan.get("as_of_at")) > moment or _aware(plan.get("created_at")) > moment:
        raise BindingValidationError("discipline plan was not available at binding time")
    if any(not bool(row.get("passed")) for row in (plan.get("quality") or [])):
        raise BindingValidationError("discipline plan failed its persisted quality gate")
    if _has_active_successor(connection, plan_id, moment):
        raise BindingValidationError("discipline plan has been superseded by an active plan")

    current = latest_binding(connection, thesis_id, account_key=account_key, symbol=symbol)
    if (current is not None
            and str(current.get("position_episode_id")) == position_episode_id
            and str(current.get("plan_id")) == str(plan_id)
            and int(current.get("thesis_revision") or 0) == thesis_revision):
        normalized = dict(current)
        normalized["revision"] = thesis_revision
        return {"status": "idempotent", "binding_id": current["binding_id"],
                "content_hash": current["content_hash"], "created_at": current["created_at"],
                "binding": normalized}
    if current is not None and str(current.get("position_episode_id")) == position_episode_id:
        prior_plan_id = str(current.get("plan_id"))
        if str(plan.get("supersedes_plan_id") or "") != prior_plan_id:
            raise BindingValidationError(
                "a different plan for the same position episode must supersede the bound plan"
            )

    binding = {
        "account_key": account_key, "symbol": symbol,
        "position_episode_id": position_episode_id, "thesis_id": thesis_id,
        "revision": thesis_revision, "plan_id": str(plan_id),
        "binding_source": binding_source, "bound_at": moment.isoformat(),
        "evidence_refs": list(evidence_refs or []),
    }
    return {**persist_binding(connection, binding), "binding": binding}


def _risk_projection(evaluation: dict[str, Any] | None) -> dict[str, Any]:
    if evaluation is None:
        return {"hard_risk": False, "plan_state": None, "triggered_lines": []}
    triggered = [dict(row) for row in (evaluation.get("line_states") or [])
                 if row.get("state") == "triggered"]
    plan_state = evaluation.get("plan_state")
    return {
        "hard_risk": plan_state == "exit_signalled"
        or any(row.get("kind") == "hard_stop" for row in triggered),
        "plan_state": plan_state,
        "triggered_lines": triggered,
    }


def load_bound_plan(connection: Any, thesis_id: str, *, account_key: str | None = None,
                    symbol: str | None = None, as_of: datetime | str | None = None) -> dict[str, Any]:
    """Load a real binding, frozen plan, and its latest PIT evaluation."""
    cutoff = _aware(as_of)
    if account_key is None:
        accounts = connection.execute("""
            SELECT DISTINCT account_key FROM quant.plan_thesis_bindings
             WHERE thesis_id=%s AND (%s::text IS NULL OR symbol=%s) AND bound_at<=%s
             ORDER BY account_key LIMIT 2
        """, (thesis_id, symbol, symbol, cutoff)).fetchall()
        if len(accounts) > 1:
            return {"status": "ambiguous_account", "thesis_id": thesis_id, "binding": None,
                    "plan": None, "latest_evaluation": None, "holding": None,
                    "plan_kind": None, "risk": {**_risk_projection(None), "actionable": False}}
    binding = latest_binding(connection, thesis_id, account_key=account_key, symbol=symbol,
                             as_of=cutoff.isoformat())
    if binding is None:
        return {"status": "unbound", "thesis_id": thesis_id, "binding": None,
                "plan": None, "latest_evaluation": None, "holding": None,
                "plan_kind": None, "risk": {**_risk_projection(None), "actionable": False}}
    binding["revision"] = int(binding["thesis_revision"])
    plan = read_plan(connection, str(binding["plan_id"]))
    if plan is None:
        return {"status": "stale", "stale_reason": "plan_missing", "binding": binding,
                "plan": None, "latest_evaluation": None, "holding": None,
                "plan_kind": None, "risk": {**_risk_projection(None), "actionable": False}}
    evaluation = connection.execute("""
        SELECT evaluation_id,plan_id,as_of_at,trading_date,basis,line_states,plan_state,
               inputs_hash,content_hash,created_at
          FROM quant.discipline_evaluations
         WHERE plan_id=%s AND as_of_at<=%s AND created_at<=%s
         ORDER BY as_of_at DESC,created_at DESC LIMIT 1
    """, (binding["plan_id"], cutoff, cutoff)).fetchone()
    evaluation = None if evaluation is None else dict(evaluation)
    stale_reason = None
    if _aware(plan.get("as_of_at")) > cutoff or _aware(plan.get("created_at")) > cutoff:
        stale_reason = "plan_not_yet_available"
    elif str(plan.get("status")) != "active":
        stale_reason = f"plan_{plan.get('status')}"
    elif any(not bool(row.get("passed")) for row in (plan.get("quality") or [])):
        stale_reason = "plan_quality_failed"
    elif _aware(plan.get("valid_until")) <= cutoff:
        stale_reason = "plan_expired"
    elif _has_active_successor(connection, str(binding["plan_id"]), cutoff):
        stale_reason = "plan_superseded"
    elif evaluation is None:
        stale_reason = "evaluation_missing"
    else:
        expected_session = _latest_settled_session(connection, cutoff)
        evaluation_day = evaluation.get("trading_date")
        if expected_session is None:
            stale_reason = "exchange_calendar_unavailable"
        elif evaluation_day is None or evaluation_day < expected_session:
            stale_reason = "evaluation_stale_session"
    plan_kind = str(plan.get("plan_kind"))
    risk = _risk_projection(evaluation)
    risk.update({
        "actionable": stale_reason is None,
        "basis": None if evaluation is None else evaluation.get("basis"),
        "trading_date": None if evaluation is None else evaluation.get("trading_date"),
        "realtime": False,
    })
    return {
        "status": "stale" if stale_reason else "bound", "stale_reason": stale_reason,
        "binding": binding, "plan": plan, "latest_evaluation": evaluation,
        "plan_kind": plan_kind,
        "holding": None if stale_reason or plan_kind != "holding" else plan.get("position"),
        "risk": risk,
    }


def apply_bound_plan(projection: dict[str, Any], bound_plan: dict[str, Any]) -> dict[str, Any]:
    """Attach the junction projection without changing thesis/evaluation semantics."""
    return {**projection, "discipline_binding": bound_plan}


__all__ = ["BindingValidationError", "apply_bound_plan", "create_binding", "load_bound_plan"]
