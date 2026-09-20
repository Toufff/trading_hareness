"""Read-only HTTP boundary for machine-derived trade discipline.

GETs only.  Plans are written by ``scripts/trade-discipline.py``; there is
deliberately no write route, because a discipline plan is an append-only
derivation from evidence rather than something a client posts, and nothing here
reaches a broker, the THS client or an order path.

* ``/plans/latest``, ``/plans/{plan_id}``, ``/evaluations/latest`` - the card;
* ``/plans/history`` - every plan of one account/symbol (stop ladder, supersede chain);
* ``/plans/{plan_id}/chart`` - the bars the plan's lines were derived from,
  in the generator's own price basis, or one session's minute bars;
* ``/plans/{plan_id}/evaluations`` - the full evaluation history per line;
* ``/reconciliations`` - stored verdicts and the real fills in a window.

Every projection is awaited from the native async read repository so a dashboard
refresh never opens a blocking transaction on the event loop.  The one optional
network read (today's licensed Longhu minute tape, when no minute bars are
stored yet) is a read of the vendor's quote, never a write.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from ..trade_discipline import chart as chart_projection

PLAN_STATUSES = ("active", "rejected_by_quality", "superseded", "expired")
DEFAULT_STATUSES = ("active", "rejected_by_quality")
BOUNDARY = "research_only_human_decision_support"
SHANGHAI = ZoneInfo("Asia/Shanghai")
SYMBOL_PATTERN = r"^\d{6}\.(SH|SZ|BJ)$"
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
RECONCILIATION_DEFAULT_DAYS = 30
RECONCILIATION_MAX_DAYS = 370


@dataclass(frozen=True)
class TradeDisciplineDependencies:
    async_database: Any
    latest_plans: Callable[..., Awaitable[list[dict[str, Any]]]]
    read_plan: Callable[..., Awaitable[dict[str, Any] | None]]
    latest_evaluation: Callable[..., Awaitable[dict[str, Any] | None]]
    plan_history: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None
    plan_evaluations: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None
    reconciliations: Callable[..., Awaitable[dict[str, list[dict[str, Any]]]]] | None = None
    daily_bars: Callable[..., Awaitable[dict[str, list[dict[str, Any]]]]] | None = None
    open_sessions: Callable[..., Awaitable[list[date]]] | None = None
    stored_minutes: Callable[..., Awaitable[dict[str, Any]]] | None = None
    # ``(symbol) -> {"session_date": "YYYY-MM-DD", "rows": [...]}``; the licensed
    # Longhu minute tape serves only its latest session.
    live_minutes: Callable[[str], Awaitable[dict[str, Any]]] | None = None
    alert_status: Callable[..., Awaitable[dict[str, Any]]] | None = None
    alert_transport_configured: Callable[[], bool] | None = None
    today: Callable[[], date] | None = None


def parse_statuses(raw: str | None) -> tuple[str, ...]:
    """``active,expired`` -> a validated tuple; an unknown status is a 422."""
    if not raw or not raw.strip():
        return DEFAULT_STATUSES
    requested = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    unknown = [status for status in requested if status not in PLAN_STATUSES]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown plan status: {','.join(unknown)}")
    return requested or DEFAULT_STATUSES


def parse_day(raw: str | None, field: str) -> date | None:
    if raw is None or raw == "":
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=f"{field} must be YYYY-MM-DD") from error


def _require(dependency: Any, name: str) -> Any:
    if dependency is None:
        raise HTTPException(status_code=503, detail=f"discipline {name} projection is not configured")
    return dependency


def _shanghai_today(deps: TradeDisciplineDependencies) -> date:
    return deps.today() if deps.today is not None else datetime.now(SHANGHAI).date()


def _json(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return str(value)


def reconciliation_payload(raw: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Verdict rows plus every fill, each fill carrying the verdicts that name it."""
    records = [_json(row) for row in raw.get("records") or []]
    verdicts_by_trade: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    for record in records:
        counts[record.get("verdict") or "unknown"] = counts.get(record.get("verdict") or "unknown", 0) + 1
        if record.get("trade_record_id"):
            verdicts_by_trade.setdefault(str(record["trade_record_id"]), []).append(
                {"verdict": record.get("verdict"), "line_kind": record.get("line_kind"),
                 "notes": record.get("notes"), "plan_id": record.get("plan_id")})
    trades = []
    for row in raw.get("trades") or []:
        trade = _json(row)
        trade["verdicts"] = verdicts_by_trade.get(str(trade.get("record_id")), [])
        trades.append(trade)
    return {"items": records, "trades": trades, "verdict_counts": counts}


def build_trade_discipline_router(deps: TradeDisciplineDependencies) -> APIRouter:
    router = APIRouter(tags=["trade-discipline"])

    @router.get("/api/v1/discipline/alerts/status")
    async def read_discipline_alert_status() -> dict[str, Any]:
        fetch = _require(deps.alert_status, "alert status")
        payload = await fetch(deps.async_database)
        configured = bool(deps.alert_transport_configured and deps.alert_transport_configured())
        return {**_json(payload), "transport_configured": configured,
                "coverage": {"minute_price_lines": "30s_during_continuous_auction",
                             "daily_price_lines": "after_authoritative_same_day_close",
                             "time_lines": "notified_when_due_by_the_active_minute_or_daily_lane"},
                "live_orders": False, "boundary": BOUNDARY}

    @router.get("/api/v1/discipline/plans/latest")
    async def read_latest_discipline_plans(
        account_key: str,
        status: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        statuses = parse_statuses(status)
        items = await deps.latest_plans(deps.async_database, account_key, statuses=statuses, limit=limit)
        return {"account_key": account_key, "statuses": list(statuses), "limit": limit,
                "count": len(items), "items": items, "live_orders": False, "boundary": BOUNDARY}

    # Declared before ``/plans/{plan_id}`` so "history" is never read as an id.
    @router.get("/api/v1/discipline/plans/history")
    async def read_discipline_plan_history(
        account_key: str = Query(min_length=1, max_length=64),
        symbol: str = Query(pattern=SYMBOL_PATTERN),
        limit: int = Query(default=200, ge=1, le=200),
    ) -> dict[str, Any]:
        fetch = _require(deps.plan_history, "history")
        items = await fetch(deps.async_database, account_key, symbol, limit=limit)
        return {"account_key": account_key, "symbol": symbol, "count": len(items), "items": items,
                "ladder": chart_projection.stop_ladder(items), "live_orders": False, "boundary": BOUNDARY}

    @router.get("/api/v1/discipline/plans/{plan_id}")
    async def read_discipline_plan(plan_id: str) -> dict[str, Any]:
        plan = await deps.read_plan(deps.async_database, plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="no discipline plan with that id")
        return {"plan": plan, "live_orders": False, "boundary": BOUNDARY}

    @router.get("/api/v1/discipline/plans/{plan_id}/chart")
    async def read_discipline_plan_chart(
        plan_id: str,
        basis: str = "daily",
        date_text: str | None = Query(default=None, alias="date", pattern=DATE_PATTERN),
    ) -> dict[str, Any]:
        if basis not in {"daily", "minute"}:
            raise HTTPException(status_code=422, detail="basis must be daily or minute")
        requested = parse_day(date_text, "date")
        plan = await deps.read_plan(deps.async_database, plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="no discipline plan with that id")
        fetch_bars = _require(deps.daily_bars, "chart")
        today = _shanghai_today(deps)
        trading_day, valid_until = chart_projection.plan_dates(plan)
        last_bar_day = min(max(valid_until, trading_day), today)
        if basis == "daily":
            raw = await fetch_bars(deps.async_database, plan["symbol"], trading_day, last_bar_day)
            first = min((chart_projection.as_day(row.get("trading_date")) for row in raw["before"]
                         if chart_projection.as_day(row.get("trading_date"))), default=trading_day)
            sessions = await _require(deps.open_sessions, "calendar")(
                deps.async_database, first, max(valid_until, trading_day))
            payload = chart_projection.daily_chart(plan, before=raw["before"], after=raw["after"],
                                                   open_days=sessions, today=today)
            return {**_json(payload), "boundary": BOUNDARY}

        # minute basis: one session, stored Longhu rows first, then today's licensed tape.
        day = requested
        if day is None:
            raw = await fetch_bars(deps.async_database, plan["symbol"], trading_day, last_bar_day)
            dates = [chart_projection.as_day(row.get("trading_date")) for row in [*raw["before"], *raw["after"]]]
            day = max((value for value in dates if value is not None), default=trading_day)
        stored = await _require(deps.stored_minutes, "minute")(deps.async_database, plan["symbol"], day)
        rows, source, reason = stored.get("rows") or [], stored.get("source"), None
        if not rows and deps.live_minutes is not None and day >= today - timedelta(days=7):
            try:
                session = await deps.live_minutes(plan["symbol"])
            except Exception as error:  # noqa: BLE001 - an absent tape is a reason, never a guess
                reason = f"longhu 实时分钟线不可用（{type(error).__name__}）"
            else:
                if str(session.get("session_date")) == day.isoformat():
                    rows, source = session.get("rows") or [], "longhu_intraday_minutes:live"
                else:
                    reason = (f"longhu 分钟接口只提供最新交易日 {session.get('session_date')}，"
                              f"{day.isoformat()} 没有已入库的分钟线")
        if not rows and reason is None:
            other = int(stored.get("other_source_rows") or 0)
            reason = (f"{day.isoformat()} 没有已入库的 longhu 分钟线"
                      + (f"（另有 {other} 行其他来源分钟线，按数据源规则不使用）" if other else ""))
        payload = chart_projection.minute_chart(plan, day=day, rows=rows, source=source, reason=reason)
        return {**_json(payload), "boundary": BOUNDARY}

    @router.get("/api/v1/discipline/plans/{plan_id}/evaluations")
    async def read_discipline_plan_evaluations(plan_id: str) -> dict[str, Any]:
        plan = await deps.read_plan(deps.async_database, plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="no discipline plan with that id")
        rows = await _require(deps.plan_evaluations, "evaluation history")(deps.async_database, plan_id)
        timeline = chart_projection.evaluation_timeline(plan, rows)
        return {"plan_id": str(plan.get("plan_id")), "plan_status": plan.get("status"), **_json(timeline),
                "live_orders": False, "boundary": BOUNDARY}

    @router.get("/api/v1/discipline/evaluations/latest")
    async def read_latest_discipline_evaluation(
        plan_id: str,
        basis: str | None = None,
    ) -> dict[str, Any]:
        if basis is not None and basis not in {"daily", "minute"}:
            raise HTTPException(status_code=422, detail="basis must be daily or minute")
        evaluation = await deps.latest_evaluation(deps.async_database, plan_id, basis=basis)
        if evaluation is None:
            raise HTTPException(status_code=404, detail="no evaluation for that plan")
        return {"evaluation": evaluation, "live_orders": False, "boundary": BOUNDARY}

    @router.get("/api/v1/discipline/reconciliations")
    async def read_discipline_reconciliations(
        account_key: str = Query(min_length=1, max_length=64),
        symbol: str | None = Query(default=None, pattern=SYMBOL_PATTERN),
        from_text: str | None = Query(default=None, alias="from", pattern=DATE_PATTERN),
        to_text: str | None = Query(default=None, alias="to", pattern=DATE_PATTERN),
    ) -> dict[str, Any]:
        end = parse_day(to_text, "to") or _shanghai_today(deps)
        start = parse_day(from_text, "from") or end - timedelta(days=RECONCILIATION_DEFAULT_DAYS)
        if start > end:
            raise HTTPException(status_code=422, detail="from must not be after to")
        if (end - start).days > RECONCILIATION_MAX_DAYS:
            raise HTTPException(status_code=422, detail=f"window must not exceed {RECONCILIATION_MAX_DAYS} days")
        raw = await _require(deps.reconciliations, "reconciliation")(
            deps.async_database, account_key, symbol=symbol, start=start, end=end)
        return {"account_key": account_key, "symbol": symbol, "from": start.isoformat(), "to": end.isoformat(),
                **reconciliation_payload(raw), "live_orders": False, "boundary": BOUNDARY}

    return router


__all__ = ["BOUNDARY", "DEFAULT_STATUSES", "PLAN_STATUSES", "TradeDisciplineDependencies",
           "build_trade_discipline_router", "parse_statuses", "reconciliation_payload"]
