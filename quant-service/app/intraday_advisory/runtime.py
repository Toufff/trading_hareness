"""One leased loop for quote acquisition, deterministic alerts and model reports."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
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
)
from .rules import QuoteSample, evaluate, sample_from_row
from .schedule import decide
from .scope import AdvisoryScope, load_scope


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


@dataclass
class RuntimeState:
    samples: dict[str, deque[QuoteSample]] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=180)))
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


async def _drain(deps: IntradayAdvisoryDependencies) -> dict[str, int]:
    rows = await deps.run_database(lambda: _load_due(deps.database))
    counts = {"attempted": 0, "sent": 0, "failed": 0, "disabled": 0}
    for row in rows:
        card = row.get("message_card") if isinstance(row.get("message_card"), dict) else {}
        outcome = await deps.post_card(card) if card else await deps.post_text(str(row["message_text"]))
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
        latest.append({
            "symbol": item.symbol, "name": item.name, "scope": item.source,
            "position_or_recommendation": item.facts,
            "quote": ({"price": sample.price, "pre_close": sample.pre_close,
                       "pct_change": round((sample.price / sample.pre_close - 1) * 100, 3),
                       "cumulative_amount": sample.amount, "observed_at": sample.observed_at.isoformat()}
                      if sample else None),
        })
    return json_safe({
        "as_of": now.isoformat(), "trigger_kind": trigger_kind, "report_kind": report_kind,
        "research_only": True, "live_orders": False,
        "scope": latest, "scope_blockers": list(scope.blockers),
        "market_context": market_context(state.index_samples, state.sector_samples),
        "recent_events": state.pending_events[-20:],
    })


async def _emit_signal(deps: IntradayAdvisoryDependencies, state: RuntimeState, signal: Any,
                       source: str, current: datetime, outcome: dict[str, Any]) -> None:
    cooldown_key = (signal.symbol, signal.kind, signal.direction, signal.severity)
    previous = state.last_signal_at.get(cooldown_key)
    if previous and current - previous < timedelta(minutes=10):
        return
    text = render_signal(signal, source=source)
    card = signal_card(signal, source=source)
    event = await deps.run_database(lambda: _persist_event_and_delivery(
        deps.database, signal, source, text, card))
    if not event:
        return
    state.last_signal_at[cooldown_key] = current
    state.pending_events.append({"event_id": str(event["event_id"]), "symbol": signal.symbol,
                                 "kind": signal.kind, "direction": signal.direction,
                                 "summary": signal.summary, "source": source,
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


async def _analyze(deps: IntradayAdvisoryDependencies, state: RuntimeState, scope: AdvisoryScope,
                   *, provider: str, trigger_kind: str, report_kind: str | None, always_push: bool) -> dict[str, Any]:
    now = deps.now()
    payload = _context(scope, state, now, trigger_kind=trigger_kind, report_kind=report_kind)
    started = now
    try:
        model = state.deepseek if provider == "deepseek" else state.codex
        if model is None:
            model = deps.deepseek_factory() if provider == "deepseek" else deps.codex_factory()
            if provider == "deepseek":
                state.deepseek = model
            else:
                state.codex = model
        result = await model.analyze(payload)
        completed = deps.now()
        stored = await deps.run_database(lambda: _persist_model_result(
            deps.database, provider=provider, trigger_kind=trigger_kind, report_kind=report_kind,
            started_at=started, completed_at=completed, input_payload=payload, output=result.output,
            status="completed"))
        should_push = always_push
        if provider == "deepseek" and not should_push:
            previous = await deps.run_database(lambda: _last_ds_fingerprint(deps.database))
            should_push = previous != result.output.get("state_fingerprint")
        if should_push:
            text = render_analysis(provider, result.output, report_kind=report_kind or trigger_kind, generated_at=completed)
            card = analysis_card(provider, result.output, report_kind=report_kind or trigger_kind,
                                 generated_at=completed)
            key = f"analysis:{provider}:{stored['analysis_run_id']}"
            await deps.run_database(lambda: _enqueue_analysis(
                deps.database, run_id=stored["analysis_run_id"], key=key, text=text, card=card))
            await _drain(deps)
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
    decision = decide(current, last_fetch=state.last_fetch, last_deepseek=state.last_deepseek,
                      last_codex=state.last_codex)
    outcome: dict[str, Any] = {"state": "idle", "scope_size": len(scope.items), "events": 0,
                               "blockers": list(scope.blockers)}
    if decision.fetch_quotes:
        session_open, reason = await deps.session_open("order_book_quote", current)
        if session_open:
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
            for row in fresh:
                sample = sample_from_row(row, current)
                if sample is None:
                    continue
                series = state.samples[sample.symbol]
                if series and (sample.observed_at <= series[-1].observed_at or sample.amount < series[-1].amount):
                    continue
                series.append(sample)
                cutoff = current - timedelta(minutes=12)
                while series and series[0].observed_at < cutoff:
                    series.popleft()
                for signal in evaluate(series):
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

    if decision.run_deepseek:
        state.last_deepseek = current
        outcome["deepseek"] = await _analyze(deps, state, scope, provider="deepseek",
                                             trigger_kind="scheduled", report_kind="ten_minute", always_push=False)
    event_due = bool(state.pending_since and current - state.pending_since >= timedelta(seconds=45))
    if decision.run_codex or event_due:
        report_kind = decision.report_kind if decision.run_codex else (
            "discipline" if any(item.get("source") == "discipline" for item in state.pending_events) else "event")
        state.last_codex = current if decision.run_codex else state.last_codex
        outcome["codex"] = await _analyze(deps, state, scope, provider="codex",
                                          trigger_kind="scheduled" if decision.run_codex else "event",
                                          report_kind=report_kind, always_push=True)
        if outcome["codex"].get("status") == "completed":
            state.pending_events.clear()
            state.pending_since = None
        elif event_due:
            state.pending_since = current  # bounded retry; never busy-loop a paid model
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
