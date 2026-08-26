"""Research-only strategy contracts for discovery, replay and Agent handoff.

This is an explicit code-reviewed registry, not dynamic plugin loading.  A
strategy remains evidence-only regardless of its maturity state; no registry
entry grants broker or order capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping


@dataclass(frozen=True)
class StrategyContract:
    key: str
    model_version: str
    owner_module: str
    input_contract: str
    evidence_datasets: tuple[str, ...]
    runtime_owner: str
    maturity: str
    live_effect: str
    description: str
    deprecated_reason: str | None = None


STRATEGY_CONTRACTS: Final[dict[str, StrategyContract]] = {
    "intraday_watchlist_confirmation": StrategyContract(
        "intraday_watchlist_confirmation", "watchlist-confirmation-v5", "app/intraday_signal_rules.py",
        "intraday-rule-input-v2", ("intraday_scan_runs", "intraday_rule_input_snapshots", "intraday_signal_events"),
        "intraday_edge", "shadow", "none", "bounded watchlist price/minute/peer confirmation research",
    ),
    "watchlist_main_wave_shadow": StrategyContract(
        "watchlist_main_wave_shadow", "watchlist-main-wave-pattern-v2", "app/watchlist_main_wave_v2.py",
        "watchlist-main-wave-v2", ("watchlist_main_wave_runs", "watchlist_main_wave_candidates"),
        "research", "shadow", "none", "post-close main-wave pattern research for the explicit watchlist",
        deprecated_reason=(
            "walk-forward roc_auc=0.4499 (2026-08-25 run) / 0.4650 (2026-08-24 run), both below the 0.5 "
            "random baseline, with selected_precision as low as 0.0526 vs a 0.1712 base rate "
            "(quant.strategy_experiments, strategy_key=watchlist_main_wave_shadow_v2). Also only ever "
            "scans the existing intraday_watchlists membership, a real selection-bias confound "
            "(event_research_post_close_backtest_v1 found base_ready_30d positive across the full "
            "market using the unrelated post_close_structures.py classifier, so main-wave's own "
            "close-only feature/label design - not \"nothing in post-close screening works\" - is the "
            "likely failure mode). Kept registered as a negative baseline, not removed."
        ),
    ),
    "countertrend_rebound_shadow": StrategyContract(
        "countertrend_rebound_shadow", "watchlist-countertrend-rebound-state-v1", "app/watchlist_countertrend_rebound.py",
        "countertrend-rebound-v1", ("watchlist_rebound_runs", "watchlist_rebound_candidates", "intraday_signal_events"),
        "research", "shadow", "none", "causal countertrend rebound research and intraday acceptance evidence",
    ),
    "ten_day_leader_rotation_shadow": StrategyContract(
        "ten_day_leader_rotation_shadow", "ten-day-leader-vwap-coordination-shadow-v1", "app/ten_day_leader_rotation_research.py",
        "ten-day-leader-rotation-v1", ("ten_day_leader_rotation_runs", "ten_day_leader_rotation_candidates", "ten_day_leader_rotation_intraday_observations"),
        "research", "shadow", "none", "post-close leader coordination pool with next-session observation",
    ),
    "post_close_base_candidates": StrategyContract(
        "post_close_base_candidates", "post-close-base-start-v1", "app/post_close_strategy_service.py",
        "same-date-close-v1", ("post_close_strategy_runs", "post_close_strategy_candidates"),
        "research", "research_enabled", "none", "same-date close candidate research with coverage gates",
    ),
    # A capacity-allocation source rather than a scored strategy: it selects
    # which scheduled-catalyst names deserve scarce intraday watchlist slots
    # and never emits a score or a direction.  See disclosure_day_watch.py for
    # the 27-session event study behind the prior-guidance exclusion.
    "disclosure_day_watch": StrategyContract(
        "disclosure_day_watch", "disclosure-day-watch-v1", "app/disclosure_day_watch.py",
        "disclosure-schedule-v1", ("disclosure_schedule", "earnings_forecasts", "earnings_express",
                                   "strategy_watchlist_proposals"),
        "research", "shadow", "none",
        "next-session scheduled disclosers without prior guidance, proposed for manual watchlist review",
    ),
    "post_close_limit_lift_pattern": StrategyContract(
        "post_close_limit_lift_pattern", "post-close-limit-lift-pattern-v6", "app/strategy_pattern_mining_service.py",
        "post-close-limit-lift-pattern-v1", ("strategy_pattern_runs", "strategy_pattern_samples"),
        "research", "research_enabled", "none", "bounded post-close minute-pattern discovery and replay evidence",
    ),
    "xiaojie_leader_flow": StrategyContract(
        "xiaojie_leader_flow", "xiaojie-leader-flow-v1", "app/xiaojie_leader_flow.py",
        "xiaojie-leader-flow-input-v1",
        ("intraday_quote_observations", "intraday_scan_runs", "strategy_pattern_samples", "analyst_observations"),
        "research", "shadow", "none",
        "point-in-time, research-only quantification of the 小杰夜报 leader/divergence/return-flow playbook",
    ),
}


def strategy_contract(key: str) -> StrategyContract:
    try:
        return STRATEGY_CONTRACTS[key]
    except KeyError as error:
        raise ValueError(f"unknown strategy contract: {key}") from error


def strategy_contract_catalog() -> list[dict[str, Any]]:
    return [
        {
            "key": item.key,
            "model_version": item.model_version,
            "owner_module": item.owner_module,
            "input_contract": item.input_contract,
            "evidence_datasets": list(item.evidence_datasets),
            "runtime_owner": item.runtime_owner,
            "maturity": item.maturity,
            "live_effect": item.live_effect,
            "description": item.description,
            "deprecated_reason": item.deprecated_reason,
        }
        for item in sorted(STRATEGY_CONTRACTS.values(), key=lambda item: item.key)
    ]


def validate_strategy_runtime_versions(runtime_versions: Mapping[str, str]) -> None:
    """Fail closed when a materialized strategy drifts from its registry contract.

    Every declared strategy must be bound deliberately by the composition root.
    The registry never grants execution authority: this guard protects evidence
    provenance and replay identity only.
    """
    declared = set(STRATEGY_CONTRACTS)
    configured = set(runtime_versions)
    missing = sorted(declared - configured)
    undeclared = sorted(configured - declared)
    if missing or undeclared:
        details = []
        if missing:
            details.append(f"missing declared strategies: {', '.join(missing)}")
        if undeclared:
            details.append(f"undeclared runtime strategies: {', '.join(undeclared)}")
        raise ValueError("strategy runtime bindings drift from registry; " + "; ".join(details))
    for key, version in runtime_versions.items():
        expected = strategy_contract(key).model_version
        if version != expected:
            raise ValueError(
                f"strategy model version mismatch for {key}: expected {expected}, received {version}",
            )


__all__ = [
    "STRATEGY_CONTRACTS", "StrategyContract", "strategy_contract", "strategy_contract_catalog",
    "validate_strategy_runtime_versions",
]
