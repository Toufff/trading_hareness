"""Bounded, provider-free replay of the locally recorded signal lifecycle.

This runner is deliberately narrower than a price/backtest engine.  It proves
that the evidence already captured by live scanning has a stable availability
order and episode lifecycle trace.  It never fetches a provider, fits a
threshold, creates an order, or treats a replay outcome as strategy alpha.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, datetime
from typing import Any, Iterable

from psycopg.types.json import Json

from .episode_lifecycle import EPISODE_CONTINUITY, session_date, signal_direction, signal_stage, strategy_family
from .intraday_replay import REPLAY_VERSION, market_event_from_row, replay_events
from .strategy_contracts import MarketEvent


ENGINE_VERSION = "intraday-signal-lifecycle-replay-v1"
MAX_REPLAY_EVENTS = 10_000


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def lifecycle_transition(state: dict[str, Any], event: MarketEvent,
                         payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replay the durable episode grouping without re-evaluating any rule.

    The signal row is itself the recorded decision event.  Re-running the
    strategy rule would require raw quotes/minutes that may not be retained for
    every historical scan; claiming that would be a false backtest.  This
    transition only verifies time ordering and lifecycle continuity.
    """
    signal = {
        "signal_key": str(payload.get("signal_key") or ""),
        "signal_type": str(payload.get("signal_type") or "watch"),
        "conditions": _as_mapping(payload.get("conditions")),
        "stage_upgrade": bool(payload.get("stage_upgrade")),
    }
    family = strategy_family(signal["signal_key"])
    direction = signal_direction(signal)
    stage = signal_stage(signal)
    key = ":".join((str(event.symbol or ""), family, str(direction), session_date(event.available_at).isoformat()))
    active = _as_mapping(state.get("active"))
    prior = _as_mapping(active.get(key))
    prior_at = _parse_iso(prior.get("last_available_at"))
    gap = event.available_at - prior_at if prior_at is not None else None
    if prior_at is None:
        lifecycle = "started"
    elif gap is not None and gap <= EPISODE_CONTINUITY:
        lifecycle = "continued"
    else:
        lifecycle = "rearmed"
    active[key] = {
        "last_available_at": event.available_at.isoformat(),
        "stage": stage,
        "last_event_state": str(payload.get("state") or "detected"),
    }
    next_state = {"active": active}
    return next_state, {
        "lifecycle": lifecycle,
        "strategy_family": family,
        "direction": direction,
        "stage": stage,
        "recorded_event_state": str(payload.get("state") or "detected"),
        "scope": "recorded_signal_lifecycle_only",
    }


def replay_recorded_signal_events(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Return a deterministic trace for an iterable of persisted event rows."""
    adapted: list[tuple[MarketEvent, int, dict[str, Any]]] = []
    for raw in rows:
        row = dict(raw)
        observed_at = row.get("observed_at")
        created_at = row.get("created_at")
        # Signal observation is the live decision's availability time.  A
        # delayed DB insert cannot make it visible earlier; use the later
        # timestamp only as ingestion evidence.
        if not isinstance(observed_at, datetime):
            raise ValueError("recorded signal replay requires observed_at")
        ingested_at = created_at if isinstance(created_at, datetime) and created_at >= observed_at else observed_at
        payload = {
            "signal_key": str(row.get("signal_key") or ""),
            "signal_type": str(row.get("signal_type") or "watch"),
            "state": str(row.get("state") or "detected"),
            "severity": str(row.get("severity") or "info"),
            "score": row.get("score"),
            "conditions": _as_mapping(row.get("conditions")),
            "risk_flags": list(row.get("risk_flags") or []),
        }
        event = market_event_from_row(
            {**row, "event_id": row.get("signal_event_id"), "event_time": observed_at,
             "available_at": observed_at, "ingested_at": ingested_at},
            source="intraday_signal_event", schema_version="intraday-signal-event-v1", payload=payload,
        )
        # Most legacy signal rows do not have a provider sequence.  Zero is a
        # valid explicit fallback because ``replay_order_key`` then resolves
        # ties by immutable event id, rather than by the caller's iterable
        # order.  Future source adapters may preserve a numeric sequence.
        try:
            source_sequence = max(0, int(row.get("source_sequence") or 0))
        except (TypeError, ValueError):
            source_sequence = 0
        adapted.append((event, source_sequence, payload))
    replay = replay_events(adapted, lifecycle_transition, initial_state={"active": {}})
    trace_outputs = [item.get("output") or {} for item in replay["trace"]]
    return {
        **replay,
        "engine_version": ENGINE_VERSION,
        "metrics": {
            "events": replay["events"],
            "symbols": len({item[0].symbol for item in adapted if item[0].symbol}),
            "strategy_families": dict(sorted(Counter(str(output.get("strategy_family") or "unknown") for output in trace_outputs).items())),
            "lifecycle_counts": dict(sorted(Counter(str(output.get("lifecycle") or "unknown") for output in trace_outputs).items())),
            "recorded_states": dict(sorted(Counter(str(output.get("recorded_event_state") or "unknown") for output in trace_outputs).items())),
        },
        "data_boundary": {
            "source": "quant.intraday_signal_events",
            "availability_clock": "observed_at",
            "ingestion_clock": "created_at_or_observed_at",
            "provider_access": "none",
            "threshold_fitting": "none",
            "orders": "none",
            "interpretation": "lifecycle reproducibility only; not a performance backtest",
        },
    }


def _input_hash(rows: Iterable[dict[str, Any]]) -> str:
    payload = [
        {
            "id": str(row.get("signal_event_id") or ""),
            "symbol": str(row.get("symbol") or ""),
            "signal_key": str(row.get("signal_key") or ""),
            "observed_at": row.get("observed_at").isoformat() if isinstance(row.get("observed_at"), datetime) else None,
            "created_at": row.get("created_at").isoformat() if isinstance(row.get("created_at"), datetime) else None,
            "conditions": _as_mapping(row.get("conditions")),
        }
        for row in rows
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_recorded_signal_lifecycle_replay(connection: Any, *, as_of_date: date | None = None,
                                          max_events: int = 5_000) -> dict[str, Any]:
    """Persist one bounded replay run using only already-stored event rows."""
    maximum = max(1, min(int(max_events), MAX_REPLAY_EVENTS))
    if as_of_date is None:
        row = connection.execute(
            "SELECT max((observed_at AT TIME ZONE 'Asia/Shanghai')::date) AS as_of_date FROM quant.intraday_signal_events"
        ).fetchone()
        as_of_date = row["as_of_date"] if row else None
    if as_of_date is None:
        rows: list[dict[str, Any]] = []
    else:
        rows = [dict(row) for row in connection.execute(
            """SELECT signal_event_id,symbol,signal_key,signal_type,severity,state,score,observed_at,created_at,
                      conditions,evidence,risk_flags
                 FROM quant.intraday_signal_events
                WHERE (observed_at AT TIME ZONE 'Asia/Shanghai')::date=%s
                ORDER BY observed_at ASC,created_at ASC,signal_event_id ASC
                LIMIT %s""",
            (as_of_date, maximum),
        ).fetchall()]
    input_hash = _input_hash(rows)
    boundary = {
        "as_of_date": str(as_of_date) if as_of_date else None,
        "max_events": maximum,
        "recorded_events_only": True,
        "provider_access": "none",
        "historical_ingestion": "none",
        "threshold_fitting": "none",
        "orders": "none",
    }
    if not rows:
        connection.execute(
            """INSERT INTO quant.intraday_replay_runs(
                    engine_version,strategy_key,strategy_version,status,input_hash,data_boundary,metrics,error_message)
                 VALUES(%s,%s,%s,'blocked',%s,%s,%s,%s)""",
            (ENGINE_VERSION, "recorded_signal_lifecycle", REPLAY_VERSION, input_hash, Json(boundary),
             Json({"events": 0}), "no locally recorded signal events for requested Shanghai date"),
        )
        return {"status": "blocked", "as_of_date": str(as_of_date) if as_of_date else None,
                "events": 0, "data_boundary": boundary}
    existing = connection.execute(
        """SELECT replay_run_id,trace_hash,metrics,data_boundary
             FROM quant.intraday_replay_runs
            WHERE engine_version=%s AND strategy_key=%s AND strategy_version=%s
              AND status='completed' AND input_hash=%s
            ORDER BY created_at DESC LIMIT 1""",
        (ENGINE_VERSION, "recorded_signal_lifecycle", REPLAY_VERSION, input_hash),
    ).fetchone()
    if existing is not None:
        # Repeated operator clicks must not append duplicate runs for the same
        # immutable event set.  A changed factor/replay implementation must
        # deliberately bump ENGINE_VERSION, producing a distinct audit run.
        return {
            "status": "completed", "reused": True, "replay_run_id": str(existing["replay_run_id"]),
            "as_of_date": str(as_of_date), "events": len(rows), "input_hash": input_hash,
            "trace_hash": str(existing["trace_hash"] or ""), "metrics": dict(existing["metrics"] or {}),
            "data_boundary": dict(existing["data_boundary"] or boundary),
        }
    replay = replay_recorded_signal_events(rows)
    # ``replay_recorded_signal_events`` exposes the full trace for
    # deterministic tests; the persisted runner deliberately does not
    # duplicate it into PostgreSQL.
    replay.pop("trace")
    # ``trace`` is intentionally not stored as unbounded duplicate raw data.
    # The input hash identifies immutable source rows; the trace hash plus
    # aggregate metrics are sufficient for golden-day regression and storage
    # remains negligible under the 40 GiB research budget.
    persisted = connection.execute(
        """INSERT INTO quant.intraday_replay_runs(
                engine_version,strategy_key,strategy_version,start_available_at,end_available_at,status,input_hash,trace_hash,
                data_boundary,metrics,error_message)
             VALUES(%s,%s,%s,%s,%s,'completed',%s,%s,%s,%s,NULL)
             RETURNING replay_run_id""",
        (ENGINE_VERSION, "recorded_signal_lifecycle", REPLAY_VERSION,
         rows[0]["observed_at"], rows[-1]["observed_at"], input_hash, replay["digest"],
         Json({**boundary, **replay["data_boundary"]}), Json(replay["metrics"])),
    ).fetchone()
    return {
        "status": "completed", "replay_run_id": str(persisted["replay_run_id"]),
        "as_of_date": str(as_of_date), "events": replay["events"],
        "input_hash": input_hash, "trace_hash": replay["digest"], "metrics": replay["metrics"],
        "data_boundary": {**boundary, **replay["data_boundary"]},
    }


__all__ = [
    "ENGINE_VERSION", "MAX_REPLAY_EVENTS", "lifecycle_transition", "replay_recorded_signal_events",
    "run_recorded_signal_lifecycle_replay",
]
