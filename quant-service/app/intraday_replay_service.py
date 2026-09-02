"""Offline replay of recorded intraday rule-input snapshots.

Both entry points read the same durable ``intraday_rule_input_snapshots``
table so a rule or policy change can be graded against exactly what the
live scanner saw, without touching current database state or sending any
alert.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from .intraday_rule_input_replay_runner import run_recorded_rule_input_replay
from .live_policy import live_policy_gate
from .paper_portfolio import paper_risk_gate
from .strategy_timing_challengers import run_challenger_backtest as run_intraday_entry_timing_challenger_backtest


def _opening_gap_window(inputs: dict[str, Any]) -> bool:
    observed_at = (inputs.get("quote") or {}).get("_scan_observed_at")
    return (
        isinstance(observed_at, datetime)
        and time(9, 30) <= observed_at.astimezone(ZoneInfo("Asia/Shanghai")).time() < time(9, 40)
    )


@dataclass(frozen=True)
class IntradayReplayDependencies:
    database: Any
    model_version: str
    signal_rules: Callable[..., list[dict[str, Any]]]


def replay_recorded_intraday_rule_inputs(
    payload: Any, dependencies: IntradayReplayDependencies,
) -> dict[str, Any]:
    def evaluate(inputs: dict[str, Any]) -> list[dict[str, Any]]:
        return dependencies.signal_rules(
            inputs["watch"], inputs["quote"], inputs["previous_quote"], inputs["daily_factors"],
            inputs["minute_features"], inputs["peer_context"],
        )

    def evaluate_policy(signal: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        """Replay the same pure risk/policy gate from snapshot-local inputs.

        V1 snapshots never call this function because they did not capture the
        required point-in-time market and portfolio values.  The generic
        replay runner labels them core-rule-only instead of reading current
        database state.
        """
        portfolio_context = dict(inputs.get("portfolio_context") or {})
        portfolio_gate = paper_risk_gate(
            signal_type=str(signal.get("signal_type") or "watch"),
            symbol=str(inputs["watch"]["symbol"]),
            position=dict(portfolio_context.get("position") or {}),
            snapshot=dict(portfolio_context.get("snapshot") or {}),
            candidate_sector_keys=list(portfolio_context.get("candidate_sector_keys") or ()),
        )
        portfolio_risk = {
            "allowed": portfolio_gate.allowed, "target_weight": portfolio_gate.target_weight,
            "reasons": list(portfolio_gate.reasons), "risk_flags": list(portfolio_gate.risk_flags),
        }
        return live_policy_gate(
            signal, inputs["watch"], inputs["quote"], inputs["daily_factors"],
            dict(inputs.get("market_context") or {}), dict(inputs.get("fast_confirmation") or {}),
            portfolio_risk,
        )

    with dependencies.database.transaction() as connection:
        return run_recorded_rule_input_replay(
            connection, as_of_date=payload.as_of_date, max_rows=payload.max_rows,
            model_version=dependencies.model_version, evaluate=evaluate, evaluate_policy=evaluate_policy,
        )


@dataclass(frozen=True)
class IntradayEntryTimingChallengerDependencies:
    database: Any
    model_version: str
    pure_signal_rules: Callable[..., list[dict[str, Any]]]
    number: Callable[[Any], float | None]
    upside_assessment: Callable[..., Any]


def run_intraday_entry_timing_challengers(
    payload: Any, dependencies: IntradayEntryTimingChallengerDependencies,
) -> dict[str, Any]:
    def evaluate_variant(inputs: dict[str, Any], overrides: dict[str, Any]) -> list[dict[str, Any]]:
        return dependencies.pure_signal_rules(
            inputs["watch"], inputs["quote"], inputs["previous_quote"], inputs["daily_factors"],
            inputs["minute_features"], inputs["peer_context"],
            number=dependencies.number, upside_assessment_fn=dependencies.upside_assessment,
            model_version=dependencies.model_version, opening_gap_window=_opening_gap_window(inputs),
            **overrides,
        )

    with dependencies.database.transaction() as connection:
        as_of_date: date | None = payload.as_of_date
        if as_of_date is None:
            row = connection.execute(
                """SELECT max((observed_at AT TIME ZONE 'Asia/Shanghai')::date) AS d
                     FROM quant.intraday_rule_input_snapshots WHERE model_version=%s""",
                (dependencies.model_version,),
            ).fetchone()
            as_of_date = row["d"] if row else None
        if as_of_date is None:
            return {"status": "blocked", "reason": "no recorded rule-input snapshots for this model version"}
        return run_intraday_entry_timing_challenger_backtest(
            connection, as_of_date, model_version=dependencies.model_version,
            evaluate_variant=evaluate_variant, max_rows=payload.max_rows,
        )


__all__ = [
    "IntradayEntryTimingChallengerDependencies",
    "IntradayReplayDependencies",
    "replay_recorded_intraday_rule_inputs",
    "run_intraday_entry_timing_challengers",
]
