"""Deterministically re-evaluate frozen core intraday rule inputs.

This is deliberately not a price backtest.  It verifies that the exact pure
``signal_rules`` implementation produces the same decision candidates from
the minimal live input bundle recorded at scan time.  Policy gates, paper
portfolio logic, fast cross-source confirmation, shadow models and orders are
outside this narrow replay boundary and remain separately auditable.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, datetime
from typing import Any, Callable, Iterable

from psycopg.types.json import Json

from .intraday_replay import REPLAY_VERSION, market_event_from_row, replay_events
from .intraday_rule_inputs import intraday_rule_input_hash, intraday_rule_replay_inputs
from .strategy_contracts import MarketEvent


ENGINE_VERSION = "intraday-rule-input-replay-v1"
STRATEGY_KEY = "intraday_signal_rules_core"
MAX_REPLAY_ROWS = 50_000

RuleEvaluator = Callable[[dict[str, Any]], list[dict[str, Any]]]


def _signal_projection(signal: dict[str, Any]) -> dict[str, Any]:
    """Keep the full pure-rule output hashable without pretending it is a fill."""
    return {
        "signal_key": str(signal.get("signal_key") or ""),
        "signal_type": str(signal.get("signal_type") or "watch"),
        "severity": str(signal.get("severity") or "info"),
        "score": signal.get("score"), "hard": bool(signal.get("hard")),
        "conditions": signal.get("conditions") if isinstance(signal.get("conditions"), dict) else {},
        "risk_flags": list(signal.get("risk_flags") or []),
    }


def _event_from_snapshot(row: dict[str, Any]) -> tuple[MarketEvent, int, dict[str, Any]]:
    observed_at = row.get("observed_at")
    created_at = row.get("created_at")
    if not isinstance(observed_at, datetime):
        raise ValueError("rule input replay requires snapshot observed_at")
    ingested_at = created_at if isinstance(created_at, datetime) and created_at >= observed_at else observed_at
    payload = dict(row.get("inputs") or {})
    stored_hash = str(row.get("input_hash") or "")
    if not stored_hash or stored_hash != intraday_rule_input_hash(payload):
        raise ValueError("rule input replay rejected a snapshot with a mismatched input_hash")
    event = market_event_from_row(
        {**row, "event_id": row.get("rule_input_snapshot_id"), "event_time": observed_at,
         "available_at": observed_at, "ingested_at": ingested_at},
        source="intraday_rule_input_snapshot", schema_version="intraday-rule-input-event-v1", payload=payload,
    )
    return event, 0, payload


def replay_recorded_rule_inputs(rows: Iterable[dict[str, Any]], *, evaluate: RuleEvaluator,
                                expected_model_version: str) -> dict[str, Any]:
    """Replay frozen core-rule inputs using an injected current pure evaluator."""
    adapted = [_event_from_snapshot(dict(row)) for row in rows]

    def transition(state: dict[str, Any], _event: MarketEvent,
                   payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        inputs = intraday_rule_replay_inputs(payload, expected_model_version=expected_model_version)
        signals = [_signal_projection(item) for item in evaluate(inputs)]
        next_state = {
            "evaluated": int(state.get("evaluated") or 0) + 1,
            "emitted": int(state.get("emitted") or 0) + len(signals),
        }
        return next_state, {"model_version": inputs["model_version"], "signals": signals}

    replay = replay_events(adapted, transition, initial_state={"evaluated": 0, "emitted": 0})
    outputs = [item.get("output") or {} for item in replay["trace"]]
    emitted = [signal for output in outputs for signal in output.get("signals") or []]
    return {
        **replay,
        "engine_version": ENGINE_VERSION,
        "metrics": {
            "snapshots": replay["events"], "emitted_signals": len(emitted),
            "symbols": len({event.symbol for event, _sequence, _payload in adapted if event.symbol}),
            "signal_keys": dict(sorted(Counter(str(signal.get("signal_key") or "unknown") for signal in emitted).items())),
        },
        "data_boundary": {
            "source": "quant.intraday_rule_input_snapshots",
            "availability_clock": "observed_at",
            "input_contract": "intraday-rule-input-v1",
            "provider_access": "none",
            "historical_ingestion": "none",
            "threshold_fitting": "none",
            "orders": "none",
            "interpretation": "core signal-rule reproducibility only; excludes policy, execution and performance claims",
        },
    }


def _input_hash(rows: Iterable[dict[str, Any]]) -> str:
    payload = [
        {"id": str(row.get("rule_input_snapshot_id") or ""), "symbol": str(row.get("symbol") or ""),
         "observed_at": row.get("observed_at").isoformat() if isinstance(row.get("observed_at"), datetime) else None,
         "model_version": str(row.get("model_version") or ""), "input_hash": str(row.get("input_hash") or "")}
        for row in rows
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def run_recorded_rule_input_replay(connection: Any, *, as_of_date: date | None,
                                   max_rows: int, model_version: str,
                                   evaluate: RuleEvaluator) -> dict[str, Any]:
    """Persist one bounded, idempotent reproducibility result from local snapshots."""
    maximum = max(1, min(int(max_rows), MAX_REPLAY_ROWS))
    if as_of_date is None:
        row = connection.execute(
            "SELECT max((observed_at AT TIME ZONE 'Asia/Shanghai')::date) AS as_of_date "
            "FROM quant.intraday_rule_input_snapshots WHERE model_version=%s", (model_version,),
        ).fetchone()
        as_of_date = row["as_of_date"] if row else None
    rows = [] if as_of_date is None else [dict(row) for row in connection.execute(
        """SELECT rule_input_snapshot_id,scan_id,symbol,observed_at,model_version,input_hash,inputs,created_at
             FROM quant.intraday_rule_input_snapshots
            WHERE (observed_at AT TIME ZONE 'Asia/Shanghai')::date=%s AND model_version=%s
            ORDER BY observed_at ASC,symbol ASC,rule_input_snapshot_id ASC LIMIT %s""",
        (as_of_date, model_version, maximum),
    ).fetchall()]
    input_hash = _input_hash(rows)
    boundary = {
        "as_of_date": str(as_of_date) if as_of_date else None, "max_rows": maximum,
        "provider_access": "none", "historical_ingestion": "none", "threshold_fitting": "none", "orders": "none",
    }
    if not rows:
        connection.execute(
            """INSERT INTO quant.intraday_replay_runs(
                   engine_version,strategy_key,strategy_version,status,input_hash,data_boundary,metrics,error_message
               ) VALUES(%s,%s,%s,'blocked',%s,%s,%s,%s)""",
            (ENGINE_VERSION, STRATEGY_KEY, model_version, input_hash, Json(boundary), Json({"snapshots": 0}),
             "no locally recorded rule-input snapshots for requested Shanghai date and model version"),
        )
        return {"status": "blocked", "as_of_date": str(as_of_date) if as_of_date else None,
                "snapshots": 0, "data_boundary": boundary}
    existing = connection.execute(
        """SELECT replay_run_id,trace_hash,metrics,data_boundary FROM quant.intraday_replay_runs
            WHERE engine_version=%s AND strategy_key=%s AND strategy_version=%s
              AND status='completed' AND input_hash=%s ORDER BY created_at DESC LIMIT 1""",
        (ENGINE_VERSION, STRATEGY_KEY, model_version, input_hash),
    ).fetchone()
    if existing is not None:
        return {"status": "completed", "reused": True, "replay_run_id": str(existing["replay_run_id"]),
                "as_of_date": str(as_of_date), "snapshots": len(rows), "input_hash": input_hash,
                "trace_hash": str(existing["trace_hash"] or ""), "metrics": dict(existing["metrics"] or {}),
                "data_boundary": dict(existing["data_boundary"] or boundary)}
    replay = replay_recorded_rule_inputs(rows, evaluate=evaluate, expected_model_version=model_version)
    replay.pop("trace")
    persisted = connection.execute(
        """INSERT INTO quant.intraday_replay_runs(
               engine_version,strategy_key,strategy_version,start_available_at,end_available_at,status,input_hash,trace_hash,
               data_boundary,metrics,error_message
           ) VALUES(%s,%s,%s,%s,%s,'completed',%s,%s,%s,%s,NULL) RETURNING replay_run_id""",
        (ENGINE_VERSION, STRATEGY_KEY, model_version, rows[0]["observed_at"], rows[-1]["observed_at"],
         input_hash, replay["digest"], Json({**boundary, **replay["data_boundary"]}), Json(replay["metrics"])),
    ).fetchone()
    return {"status": "completed", "replay_run_id": str(persisted["replay_run_id"]),
            "as_of_date": str(as_of_date), "snapshots": replay["events"], "input_hash": input_hash,
            "trace_hash": replay["digest"], "metrics": replay["metrics"],
            "data_boundary": {**boundary, **replay["data_boundary"]}}


__all__ = [
    "ENGINE_VERSION", "MAX_REPLAY_ROWS", "STRATEGY_KEY", "replay_recorded_rule_inputs",
    "run_recorded_rule_input_replay",
]
