"""Durable discipline transition state and its dedicated delivery outbox."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any

from psycopg.types.json import Json

from .alerts_evaluation import alert_line_states, line_key
from .alerts_renderer import render_discipline_alert
from .contracts import DisciplinePlan, Evaluation
from .repository import persist_evaluation


TERMINAL_LINE_STATES = frozenset({"triggered", "capped", "cancelled", "expired"})
ALERT_TARGET_STATES = frozenset({"triggered", "capped"})


def _event_key(plan_id: str, item_key: str, target: str) -> str:
    return sha256(f"{plan_id}|{item_key}|armed|{target}".encode("utf-8")).hexdigest()


def persist_evaluation_transitions(connection: Any, *, plan_id: str, plan: DisciplinePlan,
                                   evaluation: Evaluation, dashboard_url: str | None = None) -> dict[str, Any]:
    """Persist changed line state and enqueue only the first armed->target edge.

    A newly observed line is a baseline even when the stored minute tape already
    proves it triggered; that historical fact is marked ``baseline_suppressed``
    and never delivered.  Restarting after any committed step is safe because
    both the event key and its one Feishu outbox row are unique.
    """
    items = alert_line_states(plan, evaluation)
    existing_by_key: dict[str, dict[str, Any]] = {}
    for index, line, _state in items:
        key = line_key(index, line)
        row = connection.execute("""
            SELECT plan_id,line_key,state,evaluation_id,baseline_suppressed
              FROM quant.discipline_alert_line_states
             WHERE plan_id=%s AND line_key=%s FOR UPDATE""", (plan_id, key)).fetchone()
        if row:
            existing_by_key[key] = dict(row)

    changes = []
    for index, line, state in items:
        key = line_key(index, line)
        previous = existing_by_key.get(key)
        if previous is None or previous["state"] != state.state:
            changes.append((index, line, state, key, previous))
    if not changes:
        return {"evaluation_id": None, "baselines": 0, "events": [], "state_changes": 0}

    stored = persist_evaluation(connection, evaluation)
    evaluation_id = str(stored["evaluation_id"])
    events: list[dict[str, Any]] = []
    baselines = 0
    changed = 0
    for index, line, state, key, previous in changes:
        if previous is None:
            inserted = connection.execute("""
                INSERT INTO quant.discipline_alert_line_states(
                    plan_id,line_key,line_index,line_kind,state,evaluation_id,baseline_suppressed,
                    first_observed_at,last_observed_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(plan_id,line_key) DO NOTHING RETURNING state""",
                (plan_id, key, index, line.kind, state.state, evaluation_id,
                 state.state in ALERT_TARGET_STATES, evaluation.as_of_at, evaluation.as_of_at)).fetchone()
            if inserted:
                baselines += 1
                changed += 1
                continue
            previous_row = connection.execute("""
                SELECT state,evaluation_id,baseline_suppressed
                  FROM quant.discipline_alert_line_states
                 WHERE plan_id=%s AND line_key=%s FOR UPDATE""", (plan_id, key)).fetchone()
            previous = dict(previous_row) if previous_row else None
            if previous is None:
                raise RuntimeError("discipline alert line-state insert lost without a conflicting row")

        old_state = str(previous["state"])
        # A terminal fact never regresses.  In particular, a restarted provider
        # cannot re-arm a line and cause a second alert later.
        if old_state in TERMINAL_LINE_STATES:
            continue
        connection.execute("""
            UPDATE quant.discipline_alert_line_states
               SET state=%s,evaluation_id=%s,last_observed_at=%s,updated_at=now()
             WHERE plan_id=%s AND line_key=%s""",
            (state.state, evaluation_id, evaluation.as_of_at, plan_id, key))
        changed += 1
        if old_state != "armed" or state.state not in ALERT_TARGET_STATES:
            continue
        payload = {
            "plan_id": plan_id, "plan_key": plan.plan_key, "account_key": plan.account_key,
            "symbol": plan.symbol, "name": plan.name, "plan_kind": plan.plan_kind,
            "line_index": index, "line_key": key, "line": line.model_dump(mode="json"),
            "state": state.model_dump(mode="json"), "research_only": True, "live_orders": False,
        }
        event = connection.execute("""
            INSERT INTO quant.discipline_alert_events(
                event_key,plan_id,evaluation_id,line_key,line_index,line_kind,
                from_state,to_state,observed_at,payload)
            VALUES(%s,%s,%s,%s,%s,%s,'armed',%s,%s,%s)
            ON CONFLICT(event_key) DO NOTHING RETURNING event_id,event_key""",
            (_event_key(plan_id, key, state.state), plan_id, evaluation_id, key, index,
             line.kind, state.state, evaluation.as_of_at, Json(payload))).fetchone()
        if not event:
            continue
        text = render_discipline_alert(plan, line, state, dashboard_url=dashboard_url)
        delivery = connection.execute("""
            INSERT INTO quant.discipline_alert_deliveries(event_id,status,message_text,next_attempt_at)
            VALUES(%s,'pending',%s,now())
            ON CONFLICT(event_id,channel) DO NOTHING RETURNING delivery_id""",
            (event["event_id"], text)).fetchone()
        events.append({"event_id": str(event["event_id"]), "event_key": event["event_key"],
                       "delivery_id": str(delivery["delivery_id"]) if delivery else None,
                       "symbol": plan.symbol, "line_kind": line.kind, "state": state.state})
    return {"evaluation_id": evaluation_id, "baselines": baselines, "events": events,
            "state_changes": changed}


def due_deliveries(connection: Any, *, max_attempts: int = 8, limit: int = 10) -> list[dict[str, Any]]:
    rows = connection.execute("""
        SELECT d.delivery_id,d.event_id,d.message_text,d.attempt_count
          FROM quant.discipline_alert_deliveries d
         WHERE d.status IN ('pending','failed') AND d.attempt_count<%s
           AND coalesce(d.next_attempt_at,d.created_at)<=now()
           AND NOT EXISTS (
               SELECT 1 FROM quant.discipline_alert_deliveries sent
                WHERE sent.event_id=d.event_id AND sent.channel=d.channel AND sent.status='sent')
         ORDER BY d.created_at LIMIT %s""", (max_attempts, max(1, min(int(limit), 20)))).fetchall()
    return [dict(row) for row in rows]


def persist_delivery_outcome(connection: Any, delivery_id: Any, outcome: dict[str, Any], *,
                             max_attempts: int = 8) -> None:
    status = str(outcome.get("status") or "failed")
    if status not in {"sent", "failed", "disabled"}:
        status = "failed"
    connection.execute("""
        UPDATE quant.discipline_alert_deliveries
           SET status=%s,response=%s,error_message=%s,
               sent_at=CASE WHEN %s='sent' THEN now() ELSE sent_at END,
               attempt_count=attempt_count+1,
               next_attempt_at=CASE
                   WHEN %s='failed' AND attempt_count+1<%s
                   THEN now() + make_interval(
                       secs => least(900,30*power(2,least(attempt_count,5)))::double precision)
                   ELSE NULL END,
               updated_at=now()
         WHERE delivery_id=%s""",
        (status, Json(outcome.get("response") if isinstance(outcome.get("response"), dict) else {}),
         str(outcome.get("error") or outcome.get("reason") or "")[:500] or None,
         status, status, max_attempts, delivery_id))


def update_runtime_status(connection: Any, *, account_key: str, state: str,
                          started_at: datetime | None = None, completed_at: datetime | None = None,
                          session_date: Any = None, eligible_plans: int = 0, evaluated_plans: int = 0,
                          emitted_events: int = 0, reason: str | None = None,
                          error: str | None = None, details: dict[str, Any] | None = None) -> None:
    pending = connection.execute("""
        SELECT count(*)::int AS count FROM quant.discipline_alert_deliveries
         WHERE status IN ('pending','failed')""").fetchone()
    connection.execute("""
        INSERT INTO quant.discipline_alert_runtime_status(
            runtime_key,account_key,state,last_started_at,last_completed_at,last_session_date,
            eligible_plans,evaluated_plans,emitted_events,pending_deliveries,last_reason,last_error,details)
        VALUES('primary',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(runtime_key) DO UPDATE SET
            account_key=excluded.account_key,state=excluded.state,
            last_started_at=coalesce(excluded.last_started_at,quant.discipline_alert_runtime_status.last_started_at),
            last_completed_at=coalesce(excluded.last_completed_at,quant.discipline_alert_runtime_status.last_completed_at),
            last_session_date=coalesce(excluded.last_session_date,quant.discipline_alert_runtime_status.last_session_date),
            eligible_plans=excluded.eligible_plans,evaluated_plans=excluded.evaluated_plans,
            emitted_events=excluded.emitted_events,pending_deliveries=excluded.pending_deliveries,
            last_reason=excluded.last_reason,last_error=excluded.last_error,details=excluded.details,updated_at=now()""",
        (account_key, state, started_at, completed_at, session_date, eligible_plans, evaluated_plans,
         emitted_events, int((pending or {}).get("count") or 0), reason,
         str(error or "")[:500] or None, Json(details or {})))


__all__ = [
    "ALERT_TARGET_STATES", "TERMINAL_LINE_STATES", "due_deliveries", "persist_delivery_outcome",
    "persist_evaluation_transitions", "update_runtime_status",
]
