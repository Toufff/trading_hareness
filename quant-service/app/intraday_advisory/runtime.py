"""One leased loop for quote acquisition, deterministic alerts and model reports."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import replace
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import os
from typing import Any, Awaitable, Callable, Mapping
from zoneinfo import ZoneInfo

from ..agent_paper.model import ModelFailure
from ..intraday_quote_normalization import exchange_time_status
from ..json_safe_encoding import json_safe
from ..tushare_providers import safe_error_detail
from .model import CodexAdvisoryModel, DeepSeekAdvisoryModel
from .renderer import analysis_card, render_analysis, render_signal, signal_card
from .market_watch import (
    CORE_INDEX_SYMBOLS, IndexSample, SectorSample, evaluate_indices, evaluate_sectors,
    index_sample_from_row, market_context, sector_samples_from_snapshot,
)
from .repository import (
    due_deliveries, enqueue_delivery, latest_delivered_deepseek_fingerprint, latest_sector_snapshot,
    persist_analysis, persist_delivery_outcome, persist_index_samples, persist_quote_samples,
    persist_signal, recent_discipline_events, update_status,
    latest_pressure_states, latest_delivered_context,
    notification_exists, signal_delivery_suppressed,
)
from .presentation import ensure_readable_card, humanize_card, humanize_text
from .rules import QuoteSample, sample_from_row
from .pressure import feature_bundle, pressure_event, VERSION
from .delta import DELTA_KINDS, compact_facts, prepare_delta, bind_output
from .schedule import decide
from .scope import AdvisoryScope, load_scope
from .notice_policy import VERSION as NOTICE_VERSION, BRIEFING_TIMES
from .focus_technicals import technical_evidence
from ..trade_discipline.alerts_evaluation import validate_minute_tape


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_ACCOUNT_KEY = "citics-primary"

DatabaseExecutor = Callable[[Callable[[], Any]], Awaitable[Any]]


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    return str(values.get("QUANT_INTRADAY_ADVISORY_ENABLED", "false")).lower() in {"1", "true", "yes", "on"}


def account_key(environ: Mapping[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
    return str(values.get("QUANT_INTRADAY_ADVISORY_ACCOUNT_KEY") or DEFAULT_ACCOUNT_KEY).strip()


@dataclass(frozen=True)
class IntradayAdvisoryDependencies:
    database: Any
    run_database: DatabaseExecutor
    fetch_quotes: Callable[..., Awaitable[list[dict[str, Any]]]]
    fetch_indices: Callable[[list[str]], Awaitable[dict[str, dict[str, Any]]]]
    post_text: Callable[[str], Awaitable[dict[str, Any]]]
    post_card: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    session_open: Callable[..., Awaitable[tuple[bool, str]]]
    now: Callable[[], datetime]
    account_key: Callable[[], str]
    deepseek_factory: Callable[[], Any] = DeepSeekAdvisoryModel
    codex_factory: Callable[[], Any] = CodexAdvisoryModel
    dashboard_url: Callable[[], str | None] = lambda: None
    fetch_minutes: Callable[[str], Awaitable[dict[str, Any]]] | None = None


@dataclass
class RuntimeState:
    samples: dict[str, deque[QuoteSample]] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=600)))
    last_fetch: datetime | None = None
    last_deepseek: datetime | None = None
    last_codex: datetime | None = None
    last_quote_attempt_at: datetime | None = None
    last_quote_success_at: datetime | None = None
    last_quotes_received: int = 0
    last_quotes_fresh: int = 0
    last_index_fetch: datetime | None = None
    last_index_success_at: datetime | None = None
    last_indices_received: int = 0
    last_indices_fresh: int = 0
    index_samples: dict[str, deque[IndexSample]] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=60)))
    sector_samples: dict[str, deque[SectorSample]] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=15)))
    last_sector_snapshot_at: datetime | None = None
    discipline_watermark: datetime | None = None
    pending_events: list[dict[str, Any]] = field(default_factory=list)
    pending_since: datetime | None = None
    last_signal_at: dict[tuple[str, str, str, str], datetime] = field(default_factory=dict)
    deepseek: Any = None
    codex: Any = None
    analysis_task: Any = None
    analysis_events: list[dict[str,Any]] = field(default_factory=list)
    pressure_states: dict[str,Any] = field(default_factory=dict)
    pressure_day: Any = None
    last_pressure_refresh: datetime | None = None
    last_drain: datetime | None = None
    focus_last_fetch: datetime | None = None
    focus_task: Any = None
    focus_technicals: dict[str, dict[str, Any]] = field(default_factory=dict)


def _fresh(row: dict[str, Any], now: datetime) -> bool:
    status = exchange_time_status({"price_trade_time": row.get("trade_time")},
                                  now.astimezone(timezone.utc), 20)
    return status.get("status") == "fresh"


def _scope(database: Any, key: str, now: datetime) -> AdvisoryScope:
    with database.transaction() as connection:
        return load_scope(connection, account_key=key, as_of=now)


def _persist_rows(database: Any, now: datetime, rows: list[dict[str, Any]]) -> int:
    with database.transaction() as connection:
        return persist_quote_samples(connection, now, rows)


def _persist_index_rows(database: Any, now: datetime, rows: list[dict[str, Any]]) -> int:
    with database.transaction() as connection:
        return persist_index_samples(connection, now, rows)


def _latest_sector_snapshot(database: Any, now: datetime) -> dict[str, Any] | None:
    with database.transaction() as connection:
        return latest_sector_snapshot(connection, at=now)


def _pressure_states(database: Any, now: datetime):
    with database.transaction() as connection:
        return latest_pressure_states(connection, at=now)


def _delivered_context(database: Any, now: datetime):
    with database.transaction() as connection:
        return latest_delivered_context(connection,at=now)


def _persist_event_and_delivery(database: Any, signal: Any, source: str, text: str,
                                card: dict[str, Any]) -> dict[str, Any] | None:
    with database.transaction() as connection:
        event = persist_signal(connection, signal, scope_source=source)
        if event:
            enqueue_delivery(connection, key=f"signal:{event['event_key']}", kind="signal",
                             text=text, card=card, event_id=event["event_id"])
        return event


def _load_due(database: Any) -> list[dict[str, Any]]:
    with database.transaction() as connection:
        return due_deliveries(connection)


def _delivery_outcome(database: Any, delivery_id: Any, outcome: dict[str, Any]) -> None:
    with database.transaction() as connection:
        persist_delivery_outcome(connection, delivery_id, outcome)


def _notice_exists(database: Any, key: str) -> bool:
    with database.transaction() as connection:
        return notification_exists(connection, key)


def _discipline_covers(database: Any, event_id: Any, key: str) -> bool:
    with database.transaction() as connection:
        return signal_delivery_suppressed(connection, event_id, account_key=key)


async def _fetch_focus_technicals(deps: IntradayAdvisoryDependencies, symbols: list[str],
                                  current: datetime) -> dict[str, dict[str, Any]]:
    async def one(symbol: str) -> tuple[str, dict[str, Any]]:
        try:
            assert deps.fetch_minutes is not None
            session = await deps.fetch_minutes(symbol)
            tape = validate_minute_tape(session, current)
            return symbol, technical_evidence(tape)
        except Exception as error:  # noqa: BLE001 - one tape cannot stop quotes
            return symbol, {'status': 'unavailable', 'reason': safe_error_detail(str(error), 120)}
    return dict(await asyncio.gather(*(one(symbol) for symbol in symbols)))


async def _drain(deps: IntradayAdvisoryDependencies) -> dict[str, int]:
    rows = await deps.run_database(lambda: _load_due(deps.database))
    counts = {"attempted": 0, "sent": 0, "failed": 0, "disabled": 0}
    for row in rows:
        if row.get('event_id') and await deps.run_database(
                lambda: _discipline_covers(deps.database,row['event_id'],deps.account_key())):
            await deps.run_database(lambda: _delivery_outcome(deps.database,row['delivery_id'],
                {'status':'disabled','reason':'covered_by_discipline'}))
            counts['disabled'] += 1
            continue
        card = row.get("message_card") if isinstance(row.get("message_card"), dict) else {}
        try:
            if card:
                card = humanize_card(card)
                ensure_readable_card(card)
                outcome = await deps.post_card(card)
            else:
                message = humanize_text(row["message_text"])
                ensure_readable_card({"content": message})
                outcome = await deps.post_text(message)
        except ValueError as exc:
            outcome = {"status": "failed", "error": str(exc)}
        status = str(outcome.get("status") or "failed")
        if status not in counts:
            status = "failed"
        counts["attempted"] += 1
        counts[status] += 1
        await deps.run_database(lambda row=row, outcome=outcome: _delivery_outcome(deps.database, row["delivery_id"], outcome))
    return counts


def _context(scope: AdvisoryScope, state: RuntimeState, now: datetime, *,
             trigger_kind: str, report_kind: str | None) -> dict[str, Any]:
    latest = []
    for item in scope.items:
        series = state.samples.get(item.symbol)
        sample = series[-1] if series else None
        technical = state.focus_technicals.get(item.symbol) if item.facts.get('monitoring_focus') else None
        if technical and technical.get('as_of'):
            try:
                if now-datetime.fromisoformat(technical['as_of']) > timedelta(minutes=10):
                    technical = None
            except (TypeError, ValueError):
                technical = None
        latest.append({
            "symbol": item.symbol, "name": item.name, "scope": item.source,
            "position_or_recommendation": compact_facts(item.facts) if report_kind in DELTA_KINDS else item.facts,
            "windows": feature_bundle(list(series)) if series else {},
            "focus_technicals": technical,
            "quote": ({"price": sample.price, "pre_close": sample.pre_close,
                       "pct_change": round((sample.price / sample.pre_close - 1) * 100, 3),
                       "cumulative_amount": sample.amount, "observed_at": sample.observed_at.isoformat()}
                      if sample else None),
        })
    recent_events = []
    for event in state.pending_events[-20:]:
        normalized = dict(event)
        if normalized.get("summary") is not None:
            normalized["summary"] = humanize_text(normalized["summary"])
        recent_events.append(normalized)
    return json_safe({
        "as_of": now.isoformat(), "trigger_kind": trigger_kind, "report_kind": report_kind,
        "research_only": True, "live_orders": False,
        "scope": latest, "scope_blockers": list(scope.blockers),
        "market_context": market_context(state.index_samples, state.sector_samples),
        "recent_events": recent_events,
    })


async def _emit_signal(deps: IntradayAdvisoryDependencies, state: RuntimeState, signal: Any,
                       source: str, current: datetime, outcome: dict[str, Any]) -> None:
    cooldown_key = (signal.symbol, signal.kind, signal.direction, signal.severity)
    previous = state.last_signal_at.get(cooldown_key)
    if signal.kind != 'pressure_change' and previous and current - previous < timedelta(minutes=10):
        return
    text = render_signal(signal, source=source)
    card = signal_card(signal, source=source, dashboard_url=deps.dashboard_url())
    event = await deps.run_database(lambda: _persist_event_and_delivery(
        deps.database, signal, source, text, card))
    if not event:
        return
    state.last_signal_at[cooldown_key] = current
    if signal.kind == 'pressure_change':
        state.pressure_states[signal.symbol] = {'metrics':signal.metrics, 'status':'pending'}
    state.pending_events.append({"event_id": str(event["event_id"]), "symbol": signal.symbol,
                                 "kind": signal.kind, "direction": signal.direction,
                                 "summary": signal.summary, "source": source,
                                 "metrics":signal.metrics,
                                 "observed_at": signal.observed_at.isoformat()})
    state.pending_since = state.pending_since or current
    outcome["events"] += 1


def _persist_model_result(database: Any, **kwargs: Any) -> dict[str, Any]:
    with database.transaction() as connection:
        return persist_analysis(connection, **kwargs)


def _last_ds_fingerprint(database: Any) -> str | None:
    with database.transaction() as connection:
        return latest_delivered_deepseek_fingerprint(connection)


def _enqueue_analysis(database: Any, *, run_id: str, key: str, text: str, card: dict[str, Any]) -> None:
    with database.transaction() as connection:
        enqueue_delivery(connection, key=key, kind="analysis", text=text, card=card, analysis_run_id=run_id)


def _deepseek_push_worthy(output: Mapping[str, Any], previous_fingerprint: str | None) -> bool:
    """Require an explicit material-change judgment before interrupting the user.

    DeepSeek still runs and every result is persisted at its scheduled slot.
    Silence only affects Feishu delivery; it never hides the audit record.
    """
    if output.get("should_notify") is not True:
        return False
    fingerprint = str(output.get("state_fingerprint") or "").strip()
    if not fingerprint or fingerprint == previous_fingerprint:
        return False
    reason = str(output.get("notification_reason") or "").strip()
    has_focus = bool(output.get("holding_focus") or output.get("recommendation_focus"))
    has_market_risk = str(output.get("market_state") or "") == "risk"
    has_risk = bool(output.get("risks"))
    return bool(reason and (has_focus or has_market_risk or has_risk))


async def _analyze(deps: IntradayAdvisoryDependencies, state: RuntimeState, scope: AdvisoryScope,
                   *, provider: str, trigger_kind: str, report_kind: str | None, always_push: bool) -> dict[str, Any]:
    now = deps.now()
    brief_key = f"brief:{now.astimezone(SHANGHAI).date()}:{report_kind}" if report_kind in {'fixed','midday','tail'} else None
    if brief_key and await deps.run_database(lambda: _notice_exists(deps.database,brief_key)):
        return {'status':'completed','provider':provider,'pushed':False,'reason':'brief_already_queued'}
    payload = _context(scope, state, now, trigger_kind=trigger_kind, report_kind=report_kind)
    started = now
    age_limit = 360 if report_kind=='midday' else 20
    affected = {x.get('symbol') or (x.get('payload') or {}).get('symbol') for x in state.pending_events}
    required_items = [item for item in scope.items if item.symbol in affected] if report_kind in {'event','discipline'} else scope.items
    quoted = sum(1 for item in required_items if state.samples.get(item.symbol) and
                 -timedelta(seconds=5) <= now-state.samples[item.symbol][-1].observed_at <= timedelta(seconds=age_limit))
    indices = sum(1 for values in state.index_samples.values() if values and
                  now-values[-1].observed_at <= timedelta(seconds=360 if report_kind=='midday' else 90))
    required_quotes = 0 if not required_items else max(1, int(len(required_items) * 0.8 + 0.999))
    # Event interpretation needs its affected stock, not unrelated market coverage.
    if (report_kind not in {'event','discipline'} and indices < 4) or quoted < required_quotes:
        return {
            "status": "skipped", "provider": provider, "reason": "insufficient_fresh_market_context",
            "coverage": {"quoted": quoted, "required_quotes": required_quotes,
                         "indices": indices, "required_indices": 4},
        }
    try:
        baseline = await deps.run_database(lambda: _delivered_context(deps.database,now))
        if report_kind in DELTA_KINDS:
            payload = prepare_delta(payload,baseline or {})
            if not payload['scope'] and not payload['market_events']:
                return {'status':'completed','provider':provider,'pushed':False,'reason':'no_material_delta'}
        model = state.deepseek if provider == "deepseek" else state.codex
        if model is None:
            model = deps.deepseek_factory() if provider == "deepseek" else deps.codex_factory()
            if provider == "deepseek":
                state.deepseek = model
            else:
                state.codex = model
        result = await model.analyze(payload)
        completed = deps.now()
        output = bind_output(result.output,payload)
        if report_kind not in DELTA_KINDS:
            from .delta import scope_signature
            current_items = {x['symbol']:x for x in payload['scope']}
            for group in ('holding_focus','recommendation_focus'):
                output[group] = [x for x in output.get(group,[]) if
                    x['symbol'] not in (baseline or {}) or
                    scope_signature(current_items[x['symbol']]) != scope_signature(baseline[x['symbol']])]
            clocks = [x.get('quote',{}).get('observed_at') for x in payload['scope']]
            output['data_as_of'] = max((x for x in clocks if x),default=payload['as_of'])
        stored = await deps.run_database(lambda: _persist_model_result(
            deps.database, provider=provider, trigger_kind=trigger_kind, report_kind=report_kind,
            started_at=started, completed_at=completed, input_payload=payload, output=output,
            status="completed"))
        should_push = always_push
        if report_kind in DELTA_KINDS:
            should_push = output.get('should_notify') is True
            # An interpretation of a minute event is not current after a long model delay.
            if completed-started > timedelta(seconds=90):
                should_push = False
        if should_push:
            text = render_analysis(provider, output, report_kind=report_kind or trigger_kind, generated_at=completed)
            card = analysis_card(provider, output, report_kind=report_kind or trigger_kind,
                                 generated_at=completed, dashboard_url=deps.dashboard_url())
            key = brief_key or f"conditions:{now.astimezone(SHANGHAI).date()}:{output['state_fingerprint']}"
            await deps.run_database(lambda: _enqueue_analysis(
                deps.database, run_id=stored["analysis_run_id"], key=key, text=text, card=card))
            # The main loop owns transport; inference never blocks quote acquisition.
        return {"status": "completed", "provider": provider, "pushed": should_push,
                "analysis_run_id": stored["analysis_run_id"]}
    except (ModelFailure, ValueError, OSError) as error:
        completed = deps.now()
        failure = safe_error_detail(str(error), 500)
        await deps.run_database(lambda: _persist_model_result(
            deps.database, provider=provider, trigger_kind=trigger_kind, report_kind=report_kind,
            started_at=started, completed_at=completed, input_payload=payload, output=None,
            status="failed", error=failure))
        return {"status": "failed", "provider": provider, "error": failure[:300]}


def _discipline(database: Any, after: datetime) -> list[dict[str, Any]]:
    with database.transaction() as connection:
        return recent_discipline_events(connection, after=after)


def _status(database: Any, **values: Any) -> None:
    with database.transaction() as connection:
        update_status(connection, **values)


async def run_intraday_advisory_cycle(deps: IntradayAdvisoryDependencies, state: RuntimeState,
                                      *, now: datetime | None = None) -> dict[str, Any]:
    current = (now or deps.now()).astimezone(SHANGHAI)
    key = deps.account_key()
    scope = await deps.run_database(lambda: _scope(deps.database, key, current))
    if state.focus_task is not None and state.focus_task.done():
        try:
            state.focus_technicals.update(state.focus_task.result())
        except Exception as error:  # noqa: BLE001 - technical research must not stop quote acquisition
            outcome_error = safe_error_detail(str(error), 120)
            state.focus_technicals = {item.symbol: {'status': 'unavailable', 'reason': outcome_error}
                                      for item in scope.items if item.facts.get('monitoring_focus')}
        state.focus_task = None
    decision = decide(current, last_fetch=state.last_fetch, last_deepseek=state.last_deepseek,
                      last_codex=state.last_codex)
    outcome: dict[str, Any] = {"state": "idle", "scope_size": len(scope.items), "events": 0,
                               "blockers": list(scope.blockers), 'feature_version':VERSION,
                               'notification_policy':NOTICE_VERSION,
                               'briefing_times':list(BRIEFING_TIMES),
                               'event_model_followup':False}
    outcome['manual_focus_count'] = sum(item.source == 'holding' and bool(item.facts.get('monitoring_focus'))
                                        for item in scope.items)
    if state.analysis_task is not None and state.analysis_task.done():
        task, state.analysis_task = state.analysis_task, None
        try:
            outcome['analysis'] = task.result()
        except Exception as error:
            outcome['analysis'] = {'status':'failed','error':safe_error_detail(str(error),300)}
        if outcome['analysis'].get('status') == 'completed':
            state.pending_events = [x for x in state.pending_events if x not in state.analysis_events]
        state.pending_since = current if state.pending_events else None
        state.analysis_events = []
        await _drain(deps)
        state.last_drain = current
    if decision.fetch_quotes:
        session_open, reason = await deps.session_open("order_book_quote", current)
        if session_open:
            if state.pressure_day != current.date() or (state.last_pressure_refresh and current-state.last_pressure_refresh>=timedelta(seconds=60)):
                state.pressure_states = await deps.run_database(lambda: _pressure_states(deps.database,current))
                state.pressure_day = current.date()
                state.last_pressure_refresh = current
            state.last_fetch = current
            index_due = state.last_index_fetch is None or current - state.last_index_fetch >= timedelta(seconds=15)
            stock_request = deps.fetch_quotes([item.symbol for item in scope.items], max_symbols=len(scope.items)) \
                if scope.items else asyncio.sleep(0, result=[])
            index_request = deps.fetch_indices(list(CORE_INDEX_SYMBOLS)) \
                if index_due else asyncio.sleep(0, result={})
            rows, index_rows = await asyncio.gather(stock_request, index_request)
            fresh = [row for row in rows if _fresh(row, current)]
            if scope.items:
                state.last_quote_attempt_at = current
                state.last_quotes_received = len(rows)
                state.last_quotes_fresh = len(fresh)
                if fresh:
                    state.last_quote_success_at = current
            if fresh:
                await deps.run_database(lambda: _persist_rows(deps.database, current.astimezone(timezone.utc), fresh))
            source_by_symbol = {item.symbol: item.source for item in scope.items}
            focused_symbols = [item.symbol for item in scope.items if item.source == 'holding'
                               and item.facts.get('monitoring_focus')]
            if deps.fetch_minutes and focused_symbols and (state.focus_last_fetch is None or
                    current-state.focus_last_fetch >= timedelta(seconds=60)) and state.focus_task is None:
                state.focus_last_fetch = current
                state.focus_task = asyncio.create_task(_fetch_focus_technicals(deps, focused_symbols, current))
            for symbol in list(state.focus_technicals):
                if symbol not in focused_symbols:
                    del state.focus_technicals[symbol]
            for row in fresh:
                clock = exchange_time_status({'price_trade_time':row.get('trade_time')},current,20)
                observed = datetime.fromisoformat(clock['observed_trade_time'])
                sample = sample_from_row(row, observed)
                if sample is None:
                    continue
                series = state.samples[sample.symbol]
                if series and sample.observed_at <= series[-1].observed_at:
                    continue
                if series and (sample.amount < series[-1].amount or sample.volume_lot < series[-1].volume_lot):
                    series.clear()
                series.append(sample)
                cutoff = current - timedelta(minutes=45)
                while series and series[0].observed_at < cutoff:
                    series.popleft()
                signal = pressure_event(list(series),state.pressure_states.get(sample.symbol))
                if signal:
                    if sample.symbol in focused_symbols:
                        technical = state.focus_technicals.get(sample.symbol)
                        if technical and technical.get('status') == 'ready' and technical.get('as_of'):
                            try:
                                fresh_technical = current-datetime.fromisoformat(technical['as_of']) <= timedelta(minutes=3)
                            except (TypeError, ValueError):
                                fresh_technical = False
                            if fresh_technical:
                                signal = replace(signal, metrics={**signal.metrics, 'focus_technicals': technical})
                    source = source_by_symbol.get(signal.symbol, "recommendation")
                    await _emit_signal(deps, state, signal, source, current, outcome)

            if index_due:
                state.last_index_fetch = current
                state.last_indices_received = len(index_rows)
                accepted_rows: list[dict[str, Any]] = []
                for row in index_rows.values():
                    sample = index_sample_from_row(row, current)
                    if sample is None:
                        continue
                    state.index_samples[sample.symbol].append(sample)
                    accepted_rows.append(row)
                state.last_indices_fresh = len(accepted_rows)
                if accepted_rows:
                    state.last_index_success_at = current
                    await deps.run_database(lambda: _persist_index_rows(
                        deps.database, current.astimezone(timezone.utc), accepted_rows))
                if index_signal := evaluate_indices(state.index_samples):
                    await _emit_signal(deps, state, index_signal, "market_index", current, outcome)

            snapshot = await deps.run_database(lambda: _latest_sector_snapshot(deps.database, current))
            snapshot_at = snapshot.get("snapshot_minute") if snapshot else None
            if isinstance(snapshot_at, datetime) and (
                    state.last_sector_snapshot_at is None or snapshot_at > state.last_sector_snapshot_at):
                state.last_sector_snapshot_at = snapshot_at
                for sample in sector_samples_from_snapshot(snapshot):
                    state.sector_samples[sample.sector_key].append(sample)
                if sector_signal := evaluate_sectors(state.sector_samples):
                    await _emit_signal(deps, state, sector_signal, "sector", current, outcome)
            if outcome["events"]:
                outcome["delivery"] = await _drain(deps)  # deterministic text goes first
                state.last_drain = current
            outcome.update({"state": "healthy", "quotes_received": len(rows), "quotes_fresh": len(fresh),
                            "indices_received": state.last_indices_received,
                            "indices_fresh": state.last_indices_fresh})
        else:
            outcome["reason"] = reason

    watermark = state.discipline_watermark or (current - timedelta(minutes=2))
    discipline = await deps.run_database(lambda: _discipline(deps.database, watermark))
    state.discipline_watermark = current
    if discipline:
        for item in discipline:
            state.pending_events.append({"source": "discipline", **item})
        state.pending_since = state.pending_since or current

    if decision.run_deepseek and state.analysis_task is None:
        state.last_deepseek = current
        state.analysis_events = list(state.pending_events)
        state.analysis_task = asyncio.create_task(_analyze(deps, state, scope, provider="deepseek",
                                             trigger_kind="scheduled", report_kind="ten_minute", always_push=False))
    # Deterministic events and discipline already have their own cards. Do not
    # launch a second model solely because such a card was sent.
    if decision.run_codex and state.analysis_task is None:
        report_kind = decision.report_kind
        state.last_codex = current
        state.analysis_events = list(state.pending_events)
        state.analysis_task = asyncio.create_task(_analyze(deps, state, scope, provider="codex",
                                          trigger_kind="scheduled",
                                          report_kind=report_kind, always_push=True))
    state.pending_events = state.pending_events[-100:]
    await asyncio.sleep(0)  # Start the bounded inference task without awaiting its result.
    if state.last_drain is None:
        state.last_drain = current
    elif current-state.last_drain >= timedelta(seconds=30):
        await _drain(deps)
        state.last_drain = current
    # ``state`` is intentionally ``idle`` on the four local ticks between
    # five-second acquisitions.  Keep the last acquisition evidence separate
    # so an external opening guard can prove that real quotes are flowing
    # without racing the one tick whose transient state is ``healthy``.
    outcome["quote_evidence"] = {
        "attempt_at": state.last_quote_attempt_at.isoformat() if state.last_quote_attempt_at else None,
        "success_at": state.last_quote_success_at.isoformat() if state.last_quote_success_at else None,
        "received": state.last_quotes_received,
        "fresh": state.last_quotes_fresh,
        "index_success_at": state.last_index_success_at.isoformat() if state.last_index_success_at else None,
        "indices_received": state.last_indices_received,
        "indices_fresh": state.last_indices_fresh,
    }
    outcome['focus_technical_status'] = {item.symbol: state.focus_technicals.get(item.symbol, {}).get('status', 'pending')
                                         for item in scope.items if item.facts.get('monitoring_focus')}
    await deps.run_database(lambda: _status(
        deps.database, state=outcome["state"], account_key=key, now=current,
        scope_size=len(scope.items), details=outcome))
    return outcome


async def run_intraday_advisory_loop(deps: IntradayAdvisoryDependencies) -> None:
    state = RuntimeState()
    while True:
        try:
            await run_intraday_advisory_cycle(deps, state)
        except asyncio.CancelledError:
            if state.analysis_task is not None:
                state.analysis_task.cancel()
            if state.focus_task is not None:
                state.focus_task.cancel()
            raise
        except Exception as error:  # one bad provider cycle must not kill the leased loop
            now = deps.now().astimezone(SHANGHAI)
            failure = safe_error_detail(str(error), 500)
            await deps.run_database(lambda: _status(
                deps.database, state="failed", account_key=deps.account_key(), now=now,
                scope_size=0, details={}, error=failure))
        await asyncio.sleep(1)


__all__ = ["IntradayAdvisoryDependencies", "RuntimeState", "account_key", "enabled",
           "run_intraday_advisory_cycle", "run_intraday_advisory_loop"]
