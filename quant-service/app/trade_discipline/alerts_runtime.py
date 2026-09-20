"""Leased 30-second runtime for account discipline-line notifications.

The model does not watch prices.  A deterministic evaluator reads immutable
plans and the licensed minute tape, writes an idempotent transition, and only
then drains a dedicated Feishu outbox.  This module never connects to an order
path or broker UI.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time
import os
from typing import Any, Awaitable, Callable, Mapping
from zoneinfo import ZoneInfo

from ..tushare_providers import safe_error_detail
from .alerts_eligibility import load_alert_scope
from .alerts_evaluation import (
    MinuteTapeRejected, evaluate_daily_plan, evaluate_minute_plan, validate_minute_tape,
)
from .alerts_repository import (
    due_deliveries,
    persist_delivery_outcome,
    persist_evaluation_transitions,
    update_runtime_status,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_ACCOUNT_KEY = "citics-primary"
DEFAULT_INTERVAL_SECONDS = 30
MAX_FETCH_CONCURRENCY = 4


DatabaseExecutor = Callable[[Callable[[], Any]], Awaitable[Any]]
MinuteFetcher = Callable[[str], Awaitable[dict[str, Any]]]
AlertSender = Callable[[str], Awaitable[dict[str, Any]]]
SessionCheck = Callable[..., Awaitable[tuple[bool, str]]]


@dataclass(frozen=True)
class DisciplineAlertRuntimeDependencies:
    database: Any
    run_database: DatabaseExecutor
    fetch_minutes: MinuteFetcher
    post_text: AlertSender
    session_open: SessionCheck
    dashboard_url: Callable[[], str | None]
    account_key: Callable[[], str]
    now: Callable[[], datetime]
    interval_seconds: Callable[[], int]


def alerts_enabled(environ: Mapping[str, str] | None = None) -> bool:
    values = environ if environ is not None else os.environ
    return str(values.get("QUANT_DISCIPLINE_ALERTS_ENABLED", "false")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def alert_account_key(environ: Mapping[str, str] | None = None) -> str:
    values = environ if environ is not None else os.environ
    return str(values.get("QUANT_DISCIPLINE_ALERT_ACCOUNT_KEY") or DEFAULT_ACCOUNT_KEY).strip() or DEFAULT_ACCOUNT_KEY


def alert_interval_seconds(environ: Mapping[str, str] | None = None) -> int:
    values = environ if environ is not None else os.environ
    try:
        value = int(values.get("QUANT_DISCIPLINE_ALERT_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        value = DEFAULT_INTERVAL_SECONDS
    return max(DEFAULT_INTERVAL_SECONDS, min(300, value))


async def _fetch_tapes(plans: tuple[Any, ...], deps: DisciplineAlertRuntimeDependencies,
                       now: datetime) -> tuple[dict[str, Any], dict[str, str]]:
    semaphore = asyncio.Semaphore(MAX_FETCH_CONCURRENCY)

    async def one(symbol: str) -> tuple[str, Any, str | None]:
        try:
            async with semaphore:
                session = await deps.fetch_minutes(symbol)
            return symbol, validate_minute_tape(session, now), None
        except Exception as error:  # noqa: BLE001 - every rejected tape is reported per symbol
            return symbol, None, safe_error_detail(str(error), 300)

    results = await asyncio.gather(*(one(plan.symbol) for _plan_id, plan in plans))
    tapes = {symbol: tape for symbol, tape, error in results if tape is not None and error is None}
    errors = {symbol: error or "minute_tape_unavailable" for symbol, tape, error in results if tape is None}
    return tapes, errors


async def _deliver_due(deps: DisciplineAlertRuntimeDependencies) -> dict[str, int]:
    rows = await deps.run_database(lambda: _load_due(deps.database))
    counts = {"attempted": 0, "sent": 0, "failed": 0, "disabled": 0}
    for row in rows:
        outcome = await deps.post_text(str(row["message_text"]))
        counts["attempted"] += 1
        status = str(outcome.get("status") or "failed")
        if status not in {"sent", "failed", "disabled"}:
            status = "failed"
        counts[status] += 1
        await deps.run_database(lambda row=row, outcome=outcome: _persist_outcome(deps.database, row, outcome))
    return counts


def _load_due(database: Any) -> list[dict[str, Any]]:
    with database.transaction() as connection:
        return due_deliveries(connection)


def _persist_outcome(database: Any, row: dict[str, Any], outcome: dict[str, Any]) -> None:
    with database.transaction() as connection:
        persist_delivery_outcome(connection, row["delivery_id"], outcome)


def _load_scope(database: Any, account_key: str, now: datetime) -> Any:
    with database.transaction() as connection:
        return load_alert_scope(connection, account_key=account_key, as_of=now)


def _evaluate_and_persist(database: Any, plan_id: str, plan: Any, tape: Any,
                          dashboard_url: str | None) -> dict[str, Any]:
    with database.transaction() as connection:
        evaluation = evaluate_minute_plan(connection, plan_id=plan_id, plan=plan, tape=tape)
        return persist_evaluation_transitions(
            connection, plan_id=plan_id, plan=plan, evaluation=evaluation, dashboard_url=dashboard_url,
        )


def _evaluate_daily_and_persist(database: Any, plan_id: str, plan: Any, as_of: datetime,
                                dashboard_url: str | None) -> dict[str, Any]:
    with database.transaction() as connection:
        evaluation = evaluate_daily_plan(connection, plan_id=plan_id, plan=plan, as_of=as_of)
        return persist_evaluation_transitions(
            connection, plan_id=plan_id, plan=plan, evaluation=evaluation, dashboard_url=dashboard_url,
        )


def _write_status(database: Any, **values: Any) -> None:
    with database.transaction() as connection:
        update_runtime_status(connection, **values)


def _calendar_open(database: Any, day: date) -> bool | None:
    with database.transaction() as connection:
        row = connection.execute(
            """SELECT is_open FROM quant.market_trade_calendar
                 WHERE exchange='SSE' AND calendar_date=%s""", (day,),
        ).fetchone()
    return None if row is None else bool(row["is_open"])


def _daily_cycle_completed(database: Any, account_key: str, day: date) -> bool:
    with database.transaction() as connection:
        row = connection.execute(
            """SELECT state,last_session_date,details
                 FROM quant.discipline_alert_runtime_status
                WHERE runtime_key='primary' AND account_key=%s""", (account_key,),
        ).fetchone()
    if not row or row["last_session_date"] != day:
        return False
    details = row.get("details") if hasattr(row, "get") else row["details"]
    return bool(isinstance(details, dict) and details.get("daily_completed"))


async def _record_idle(deps: DisciplineAlertRuntimeDependencies, *, account_key: str,
                       started: datetime, reason: str, delay: int = 60) -> dict[str, Any]:
    delivery = await _deliver_due(deps)
    await deps.run_database(lambda: _write_status(
        deps.database, account_key=account_key, state="idle", started_at=started,
        completed_at=deps.now(), session_date=started.astimezone(SHANGHAI).date(), reason=reason,
        details={"delivery": delivery, "live_orders": False, "research_only": True,
                 "next_delay_seconds": delay},
    ))
    return {"status": "idle", "reason": reason, "eligible": 0, "evaluated": 0,
            "events": 0, "delivery": delivery, "next_delay_seconds": delay}


async def run_discipline_alert_cycle(deps: DisciplineAlertRuntimeDependencies, *,
                                     now: datetime | None = None) -> dict[str, Any]:
    """Run one live-session cycle; public for deterministic acceptance tests."""
    started = now or deps.now()
    account_key = deps.account_key()
    active, session_reason = await deps.session_open(now=started)
    if not active:
        local = started.astimezone(SHANGHAI)
        # Daily price lines are a separate bounded lane: no unfinished candle
        # is read, and the canonical bar gate below keeps retrying fail-closed
        # until the authoritative close is actually available.
        if local.time() < time(15, 5):
            return await _record_idle(
                deps, account_key=account_key, started=started, reason=session_reason, delay=60,
            )
        calendar_open = await deps.run_database(lambda: _calendar_open(deps.database, local.date()))
        if calendar_open is False or (calendar_open is None and local.weekday() >= 5):
            return await _record_idle(
                deps, account_key=account_key, started=started, reason="exchange_calendar_closed", delay=900,
            )
        if calendar_open is None:
            delivery = await _deliver_due(deps)
            await deps.run_database(lambda: _write_status(
                deps.database, account_key=account_key, state="blocked", started_at=started,
                completed_at=deps.now(), session_date=local.date(), reason="trade_calendar_unavailable",
                details={"coverage": {"basis": "daily", "calendar": "missing"}, "delivery": delivery,
                         "live_orders": False, "research_only": True, "next_delay_seconds": 300},
            ))
            return {"status": "blocked", "reason": "trade_calendar_unavailable", "eligible": 0,
                    "evaluated": 0, "events": 0, "delivery": delivery, "next_delay_seconds": 300}
        already_completed = await deps.run_database(
            lambda: _daily_cycle_completed(deps.database, account_key, local.date()))
        if already_completed:
            # Preserve the completed marker in the durable status row.  Writing
            # a fresh idle projection here would erase ``daily_completed`` and
            # cause the next poll to evaluate the same close again.
            delivery = await _deliver_due(deps)
            return {"status": "idle", "reason": "daily_cycle_already_completed", "eligible": 0,
                    "evaluated": 0, "events": 0, "delivery": delivery, "next_delay_seconds": 300}
        return await run_discipline_alert_daily_cycle(deps, now=started)

    await deps.run_database(lambda: _write_status(
        deps.database, account_key=account_key, state="running", started_at=started,
        session_date=started.astimezone(SHANGHAI).date(), reason=session_reason,
    ))
    try:
        scope = await deps.run_database(lambda: _load_scope(deps.database, account_key, started))
        minute_plans = tuple(
            (plan_id, plan) for plan_id, plan in scope.plans
            if any(line.execute_by == "price" and line.confirm.basis == "minute" for line in plan.lines)
        )
        tapes, tape_errors = await _fetch_tapes(minute_plans, deps, started)
        evaluated = 0
        emitted = 0
        baselines = 0
        evaluation_errors: dict[str, str] = {}
        dashboard_url = deps.dashboard_url()
        for plan_id, plan in minute_plans:
            tape = tapes.get(plan.symbol)
            if tape is None:
                continue
            try:
                result = await deps.run_database(
                    lambda plan_id=plan_id, plan=plan, tape=tape: _evaluate_and_persist(
                        deps.database, plan_id, plan, tape, dashboard_url,
                    ))
            except (MinuteTapeRejected, ValueError, RuntimeError) as error:
                evaluation_errors[plan.symbol] = safe_error_detail(str(error), 300)
                continue
            evaluated += 1
            emitted += len(result["events"])
            baselines += int(result["baselines"])
        delivery = await _deliver_due(deps)
        errors = {**tape_errors, **evaluation_errors}
        coverage_gaps = bool(scope.blockers)
        state = ("blocked" if coverage_gaps and not minute_plans else
                 "degraded" if errors or coverage_gaps else "healthy")
        reason = ("authoritative_scope_unavailable" if state == "blocked" else
                  "partial_scope_or_plan_failed_closed" if state == "degraded" else "cycle_completed")
        details = {
            "snapshot_id": scope.snapshot_id,
            "recommendation_decision_id": scope.recommendation_decision_id,
            "scope_blockers": list(scope.blockers), "excluded": list(scope.excluded),
            "plan_errors": errors, "baselines": baselines, "delivery": delivery,
            "coverage": {"basis": "minute", "eligible_total": len(scope.plans),
                         "eligible_with_minute_lines": len(minute_plans)},
            "live_orders": False, "research_only": True,
        }
        completed = deps.now()
        await deps.run_database(lambda: _write_status(
            deps.database, account_key=account_key, state=state, started_at=started,
            completed_at=completed, session_date=started.astimezone(SHANGHAI).date(),
            eligible_plans=len(minute_plans), evaluated_plans=evaluated, emitted_events=emitted,
            reason=reason, details=details,
        ))
        return {"status": state, "reason": reason, "eligible": len(minute_plans),
                "evaluated": evaluated, "events": emitted, "delivery": delivery, "errors": errors,
                "next_delay_seconds": deps.interval_seconds()}
    except Exception as error:  # noqa: BLE001 - durable status precedes supervisor restart
        message = safe_error_detail(str(error), 500)
        await deps.run_database(lambda: _write_status(
            deps.database, account_key=account_key, state="failed", started_at=started,
            completed_at=deps.now(), session_date=started.astimezone(SHANGHAI).date(),
            reason="cycle_failed", error=message,
            details={"live_orders": False, "research_only": True},
        ))
        raise


async def run_discipline_alert_daily_cycle(deps: DisciplineAlertRuntimeDependencies, *,
                                           now: datetime | None = None) -> dict[str, Any]:
    """Evaluate daily price lines once their same-day canonical bars exist."""
    started = now or deps.now()
    local = started.astimezone(SHANGHAI)
    close_at = datetime.combine(local.date(), time(15, 0), tzinfo=SHANGHAI)
    account_key = deps.account_key()
    calendar_open = await deps.run_database(lambda: _calendar_open(deps.database, local.date()))
    if calendar_open is not True:
        reason = "exchange_calendar_closed" if calendar_open is False else "trade_calendar_unavailable"
        state = "idle" if calendar_open is False else "blocked"
        delivery = await _deliver_due(deps)
        await deps.run_database(lambda: _write_status(
            deps.database, account_key=account_key, state=state, started_at=started,
            completed_at=deps.now(), session_date=local.date(), reason=reason,
            details={"coverage": {"basis": "daily", "calendar": calendar_open}, "delivery": delivery,
                     "live_orders": False, "research_only": True, "next_delay_seconds": 300},
        ))
        return {"status": state, "reason": reason, "eligible": 0, "evaluated": 0,
                "events": 0, "delivery": delivery, "next_delay_seconds": 300}
    scope = await deps.run_database(lambda: _load_scope(deps.database, account_key, close_at))
    daily_plans = tuple(
        (plan_id, plan) for plan_id, plan in scope.plans
        if any(line.execute_by == "price" and line.confirm.basis == "daily" for line in plan.lines)
    )
    evaluated = emitted = baselines = 0
    errors: dict[str, str] = {}
    dashboard_url = deps.dashboard_url()
    for plan_id, plan in daily_plans:
        try:
            result = await deps.run_database(
                lambda plan_id=plan_id, plan=plan: _evaluate_daily_and_persist(
                    deps.database, plan_id, plan, close_at, dashboard_url,
                ))
        except (MinuteTapeRejected, ValueError, RuntimeError) as error:
            errors[plan.symbol] = safe_error_detail(str(error), 300)
            continue
        evaluated += 1
        emitted += len(result["events"])
        baselines += int(result["baselines"])
    delivery = await _deliver_due(deps)
    coverage_gaps = bool(scope.blockers)
    state = ("blocked" if errors or (coverage_gaps and not daily_plans) else
             "degraded" if coverage_gaps else "healthy")
    reason = ("authoritative_daily_data_not_ready" if errors else
              "authoritative_scope_unavailable" if state == "blocked" else
              "partial_authoritative_scope" if state == "degraded" else "daily_cycle_completed")
    details = {
        "snapshot_id": scope.snapshot_id,
        "recommendation_decision_id": scope.recommendation_decision_id,
        "scope_blockers": list(scope.blockers), "excluded": list(scope.excluded),
        "plan_errors": errors, "baselines": baselines, "delivery": delivery,
        "coverage": {"basis": "daily", "eligible_total": len(scope.plans),
                     "eligible_with_daily_lines": len(daily_plans)},
        "daily_completed": not errors and not (coverage_gaps and not daily_plans),
        "live_orders": False, "research_only": True, "next_delay_seconds": 300,
    }
    await deps.run_database(lambda: _write_status(
        deps.database, account_key=account_key, state=state, started_at=started,
        completed_at=deps.now(), session_date=local.date(), eligible_plans=len(daily_plans),
        evaluated_plans=evaluated, emitted_events=emitted, reason=reason, details=details,
    ))
    return {"status": state, "reason": reason, "eligible": len(daily_plans),
            "evaluated": evaluated, "events": emitted, "delivery": delivery, "errors": errors,
            "next_delay_seconds": 300}


async def run_discipline_alert_loop(deps: DisciplineAlertRuntimeDependencies) -> None:
    while True:
        outcome = await run_discipline_alert_cycle(deps)
        delay = int(outcome.get("next_delay_seconds") or (
            deps.interval_seconds() if outcome["status"] != "idle" else max(60, deps.interval_seconds())))
        await asyncio.sleep(delay)


__all__ = [
    "DEFAULT_ACCOUNT_KEY", "DEFAULT_INTERVAL_SECONDS", "DisciplineAlertRuntimeDependencies",
    "alert_account_key", "alert_interval_seconds", "alerts_enabled", "run_discipline_alert_cycle",
    "run_discipline_alert_daily_cycle", "run_discipline_alert_loop",
]
