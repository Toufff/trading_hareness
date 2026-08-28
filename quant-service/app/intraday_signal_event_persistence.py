"""Persist already-generated intraday candidates inside the scan transaction.

Candidate generation is deliberately separate from this module.  This layer
adds the frozen policy/risk context, advances the existing event state machine
and writes the audit/paper-decision evidence, but never opens a transaction or
contacts a provider itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable
import uuid

from .stable_json import tolerant_json


@dataclass(frozen=True)
class IntradaySignalEventPersistenceDependencies:
    paper_risk_gate: Callable[..., Any]
    live_policy_gate: Callable[..., dict[str, Any]]
    classify_setup_state: Callable[..., dict[str, Any]]
    factor_contracts: Callable[[dict[str, Any]], list[dict[str, Any]]]
    probability: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    decision_context: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    signal_contract: Callable[[dict[str, Any], datetime], dict[str, Any]]
    event_state: Callable[..., str]
    ensure_episode: Callable[..., dict[str, Any] | None]
    attribution: Callable[..., dict[str, Any]]
    paper_decision_payload: Callable[..., dict[str, Any]]
    persist_paper_decision: Callable[..., Any]


def persist_generated_signals(
    connection: Any,
    *,
    scan_id: uuid.UUID,
    observed_at: datetime,
    symbol: str,
    watch: dict[str, Any],
    quote: dict[str, Any] | None,
    daily_factors: dict[str, Any],
    minute_feature: dict[str, Any] | None,
    peer_context: dict[str, Any] | None,
    market_context: dict[str, Any],
    fast_confirmation: dict[str, Any],
    order_book_feature: dict[str, Any],
    tushare_minute: dict[str, Any] | None,
    paper_position: dict[str, Any] | None,
    portfolio_snapshot: dict[str, Any],
    candidate_sector_keys: tuple[str, ...] | list[str],
    probability_profiles: dict[str, Any],
    generated_signals: list[dict[str, Any]],
    existing_event_state: Any,
    confirmation_window: timedelta,
    factor_contract_version: str,
    dependencies: IntradaySignalEventPersistenceDependencies,
) -> list[dict[str, Any]]:
    """Apply existing policy/state semantics and write event evidence.

    ``connection`` belongs to the caller's single scan transaction.  Mutating
    ``existing_event_state.latest_by_key`` after each write preserves the old
    same-scan duplicate-key confirmation behavior.
    """
    persisted: list[dict[str, Any]] = []
    for signal in generated_signals:
        signal.setdefault("symbol", symbol)
        signal.setdefault("observed_at", observed_at)
        signal["conditions"] = {**signal["conditions"], "realtime_cross_check": fast_confirmation}
        if fast_confirmation.get("status") == "mismatch":
            signal["risk_flags"] = [*signal["risk_flags"], "realtime_cross_source_price_mismatch"]
        portfolio_gate = dependencies.paper_risk_gate(
            signal_type=signal["signal_type"], symbol=symbol, position=paper_position,
            snapshot=portfolio_snapshot, candidate_sector_keys=candidate_sector_keys,
        )
        portfolio_risk = {
            "allowed": portfolio_gate.allowed, "target_weight": portfolio_gate.target_weight,
            "reasons": list(portfolio_gate.reasons), "risk_flags": list(portfolio_gate.risk_flags),
        }
        policy = dependencies.live_policy_gate(
            signal, watch, quote, daily_factors, market_context, fast_confirmation, portfolio_risk,
        )
        setup_state = dependencies.classify_setup_state(watch, quote, minute_feature, peer_context, policy)
        signal["conditions"] = {
            **signal["conditions"], "policy_gate": policy, "setup_state": setup_state,
            "order_book_proxy": order_book_feature,
            "factor_contract_version": factor_contract_version,
            "factor_contracts": dependencies.factor_contracts(signal),
        }
        signal["risk_flags"] = [*signal["risk_flags"], *policy["risk_flags"]]
        probability = dependencies.probability(signal, probability_profiles)
        signal["conditions"] = {
            **signal["conditions"], "decision_context": dependencies.decision_context(signal, probability),
        }
        signal["conditions"] = {
            **signal["conditions"], "signal_contract": dependencies.signal_contract(signal, observed_at),
        }
        latest = existing_event_state.latest_by_key.get(str(signal["signal_key"]))
        last_key_alerted = existing_event_state.last_alerted_by_key.get(str(signal["signal_key"]))
        last_symbol_watch_alerted = existing_event_state.last_symbol_watch_alerted
        state = dependencies.event_state(
            signal, observed_at=observed_at,
            latest_event_at=latest["observed_at"] if latest else None,
            last_key_alerted_at=last_key_alerted["observed_at"] if last_key_alerted else None,
            last_symbol_watch_alerted_at=(last_symbol_watch_alerted["observed_at"] if last_symbol_watch_alerted else None),
            last_key_alert=dict(last_key_alerted) if last_key_alerted else None,
        )
        if signal.get("shadow_only"):
            state = "suppressed"
        if state == "confirmed" and fast_confirmation.get("status") == "mismatch":
            state = "confirming"
        if state == "confirmed" and not policy["allow_confirmation"]:
            state = "confirming"
        episode = None if signal["signal_type"] == "data_issue" else dependencies.ensure_episode(
            connection, signal, observed_at, state, symbol=symbol,
        )
        signal["conditions"] = {**signal["conditions"], "episode": episode or {"state": "not_applicable"}}
        evidence = {
            "tencent": quote, "tencent_order_book": order_book_feature, "tencent_minute": minute_feature,
            "peer_context": peer_context, "tushare_rt_min": tushare_minute,
            "tushare_rt_k_fast": fast_confirmation, "daily_factors": daily_factors,
            "market_context": market_context,
        }
        evidence["attribution"] = dependencies.attribution(
            signal["signal_key"], signal["signal_type"], signal["conditions"], evidence, market_context,
        )
        event = connection.execute(
            """INSERT INTO quant.intraday_signal_events(
                 scan_id,symbol,signal_key,signal_type,severity,state,score,observed_at,expires_at,
                 conditions,evidence,risk_flags,episode_id,material_state_hash,stage)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING signal_event_id""",
            (scan_id, symbol, signal["signal_key"], signal["signal_type"], signal["severity"], state,
             signal["score"], observed_at, observed_at + confirmation_window,
             # ``evidence`` is assembled from rule inputs that came out of the
             # database, so it can carry a Decimal or a datetime the stdlib
             # encoder has no hook for.  Nothing here hashes the stored text,
             # so the tolerant adapter is safe and key order is untouched.
             tolerant_json(signal["conditions"]), tolerant_json(evidence),
             tolerant_json(signal["risk_flags"]),
             episode["episode_id"] if episode else None,
             episode["material_state_hash"] if episode else None,
             episode["stage"] if episode else "data_issue"),
        ).fetchone()
        existing_event_state.latest_by_key[str(signal["signal_key"])] = {"observed_at": observed_at}
        if state == "confirmed":
            paper_payload = dependencies.paper_decision_payload(signal, state, policy, portfolio_risk)
            dependencies.persist_paper_decision(connection, event["signal_event_id"], paper_payload)
            if not portfolio_gate.allowed:
                connection.execute(
                    """INSERT INTO quant.paper_risk_events(decision_id,symbol,event_type,severity,message,occurred_at,details)
                       SELECT decision_id,%s,'portfolio_limit','block',%s,%s,%s::jsonb
                         FROM quant.paper_decisions
                        WHERE signal_event_id=%s ORDER BY created_at DESC LIMIT 1""",
                    (symbol, "; ".join(portfolio_gate.reasons), observed_at,
                     tolerant_json({"risk_flags": list(portfolio_gate.risk_flags)}),
                     event["signal_event_id"]),
                )
        persisted.append({
            "signal_event_id": event["signal_event_id"], "symbol": symbol, "state": state,
            **signal, "observed_at": observed_at, "quote": quote,
            "minute": (tushare_minute or {}).get("latest"),
            "fast_quote_confirmation": fast_confirmation, "watch": watch,
        })
    return persisted


__all__ = ["IntradaySignalEventPersistenceDependencies", "persist_generated_signals"]
