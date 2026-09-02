"""Storage and replay contracts for strategy evidence datasets.

The registry describes data placement; it does not grant a dataset decision
eligibility or change a strategy threshold. Cloud copies are immutable replay
evidence and must be restored through schema/hash validation before research
code consumes them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Iterable, Mapping


@dataclass(frozen=True)
class DataProductContract:
    key: str
    layer: str
    time_basis: str
    partition_keys: tuple[str, ...]
    archive_format: str
    cloud_retention: str
    local_tier: str
    local_hot_window_days: int | None
    replay_role: str
    description: str


def _product(
    key: str,
    layer: str,
    time_basis: str,
    partition_keys: tuple[str, ...],
    *,
    archive_format: str = "parquet_zstd",
    local_tier: str = "warm",
    local_hot_window_days: int | None = 120,
    replay_role: str = "point_in_time_replay",
    description: str,
) -> DataProductContract:
    return DataProductContract(
        key=key,
        layer=layer,
        time_basis=time_basis,
        partition_keys=partition_keys,
        archive_format=archive_format,
        cloud_retention="indefinite_immutable",
        local_tier=local_tier,
        local_hot_window_days=local_hot_window_days,
        replay_role=replay_role,
        description=description,
    )


_PRODUCTS = (
    # L0: immutable provider/source evidence. JSONL keeps provider payloads
    # lossless; canonical tables below carry the compact analytical shape.
    _product("raw_market_observations", "raw", "available_at", ("provider_key", "capability", "exchange_date", "hour"), archive_format="jsonl_zstd", local_tier="cold_cache", local_hot_window_days=30, replay_role="source_audit", description="raw normalized and provider payload evidence"),
    _product("tushare_raw_records", "raw", "available_at", ("provider_key", "api_name", "available_date"), archive_format="jsonl_zstd", local_tier="cold_cache", local_hot_window_days=30, replay_role="source_audit", description="licensed provider row evidence with request identity"),
    _product("remote_reports", "raw", "report_date+first_synced_at", ("report_year", "source_key"), archive_format="jsonl_zstd", local_tier="warm", local_hot_window_days=365, replay_role="analyst_source_replay", description="remote analyst reports before extraction"),
    _product("remote_analyst_messages", "raw", "received_at", ("received_date", "source_key"), archive_format="jsonl_zstd", local_tier="warm", local_hot_window_days=365, replay_role="analyst_source_replay", description="remote analyst messages before extraction"),

    # L1: canonical market and point-in-time reference data. Daily bars and
    # controls stay local because nearly every research query reuses them.
    _product("canonical_bars_daily", "canonical", "trading_date+available_at", ("exchange", "trading_year", "symbol_bucket"), local_tier="hot", local_hot_window_days=None, replay_role="daily_market_replay", description="point-in-time canonical adjusted daily bars"),
    _product("daily_fundamentals", "canonical", "trading_date+available_at", ("exchange", "trading_year", "symbol_bucket"), local_tier="hot", local_hot_window_days=None, replay_role="daily_fundamental_replay", description="daily valuation, turnover and share controls"),
    _product("daily_trade_limits", "canonical", "trading_date+available_at", ("exchange", "trading_year", "symbol_bucket"), local_tier="hot", local_hot_window_days=None, replay_role="entry_feasibility_replay", description="point-in-time daily limit controls"),
    _product("daily_adjustment_factors", "canonical", "trading_date+available_at", ("exchange", "trading_year", "symbol_bucket"), local_tier="hot", local_hot_window_days=None, replay_role="price_adjustment_replay", description="point-in-time corporate-action adjustment factors"),
    _product("stock_money_flow_daily", "canonical", "trading_date+available_at", ("trading_year", "symbol_bucket"), local_tier="hot", local_hot_window_days=None, replay_role="daily_flow_replay", description="daily stock-flow evidence with provider provenance"),
    _product("market_bars_minute", "canonical", "bar_time+source_available_at", ("exchange_date", "sample_role", "symbol_bucket", "hour"), local_tier="warm", local_hot_window_days=120, replay_role="minute_tape_replay", description="canonical minute tape for event, near-threshold and matched-control replay"),
    _product("intraday_minute_sessions", "canonical", "bar_time+available_at", ("exchange_date", "symbol_bucket", "hour"), local_tier="hot", local_hot_window_days=60, replay_role="time_of_day_replay", description="captured watch-universe minute sessions"),
    _product("intraday_quote_observations", "canonical", "observed_at", ("exchange_date", "source_name", "symbol_bucket", "hour"), local_tier="hot", local_hot_window_days=30, replay_role="watch_quote_replay", description="watch-universe quote observations"),
    _product("intraday_fast_quotes", "canonical", "observed_at", ("exchange_date", "symbol_bucket", "hour"), local_tier="hot", local_hot_window_days=20, replay_role="secondary_quote_confirmation", description="high-frequency bounded quote confirmations"),
    _product("intraday_order_book_observations", "canonical", "observed_at", ("exchange_date", "symbol_bucket", "hour"), local_tier="hot", local_hot_window_days=20, replay_role="microstructure_replay", description="bounded watch-universe order-book observations"),
    _product("intraday_board_flow_snapshots", "features", "observed_at", ("exchange_date", "hour"), local_tier="hot", local_hot_window_days=120, replay_role="sector_regime_replay", description="board-flow curve snapshots and coverage"),
    _product("sector_membership_history", "canonical", "valid_from+known_at", ("provider_key", "valid_year", "board_bucket"), local_tier="hot", local_hot_window_days=None, replay_role="point_in_time_sector_mapping", description="as-known-at sector membership history"),
    _product("sector_member_sync_state", "control", "trading_date+updated_at", ("taxonomy_key", "trading_year"), archive_format="jsonl_zstd", local_tier="hot", local_hot_window_days=None, replay_role="membership_coverage_audit", description="per-board member-sync completion, failures and retry evidence"),
    _product("market_events", "raw", "occurred_at+available_at", ("event_type", "exchange_date"), archive_format="jsonl_zstd", local_tier="warm", local_hot_window_days=365, replay_role="auction_and_limit_event_audit", description="provider auction, limit-pool and market-event evidence including no-signal context"),
    _product("sentiment_cycle_daily", "features", "trading_date+calculated_at", ("trading_year", "model_version"), local_tier="hot", local_hot_window_days=None, replay_role="limit_ladder_and_cycle_replay", description="daily sealed/broken-board ladder and prior-limit premium evidence"),
    _product("disclosure_schedule", "canonical", "period+available_at", ("period_year", "symbol_bucket"), local_tier="hot", local_hot_window_days=None, replay_role="event_schedule_replay", description="point-in-time disclosure schedule"),
    _product("earnings_forecasts", "canonical", "ann_date+available_at", ("announcement_year", "symbol_bucket"), local_tier="hot", local_hot_window_days=None, replay_role="event_fundamental_replay", description="earnings forecast evidence"),
    _product("earnings_express", "canonical", "ann_date+available_at", ("announcement_year", "symbol_bucket"), local_tier="hot", local_hot_window_days=None, replay_role="event_fundamental_replay", description="earnings express evidence"),

    # L2: frozen feature and rule inputs. Cloud capacity lets us retain
    # negative/no-signal snapshots as causal controls too.
    _product("feature_snapshots", "features", "as_of_date+created_at", ("feature_version", "as_of_year", "symbol_bucket"), local_tier="warm", local_hot_window_days=365, replay_role="feature_replay", description="versioned daily feature snapshots"),
    _product("intraday_rule_input_snapshots", "features", "observed_at", ("model_version", "exchange_date", "symbol_bucket", "hour"), local_tier="hot", local_hot_window_days=120, replay_role="exact_rule_replay", description="hash-verified causal inputs for intraday rules"),
    _product("intraday_scan_runs", "features", "observed_at", ("exchange_date", "model_version"), local_tier="hot", local_hot_window_days=365, replay_role="scan_universe_replay", description="scan coverage and source-state envelope"),
    _product("l2_incremental_value_evaluations", "research", "evaluated_at", ("algorithm_version", "exchange_date"), archive_format="jsonl_zstd", local_tier="warm", local_hot_window_days=365, replay_role="microstructure_promotion_gate", description="matched minute evidence for fail-closed L2 expansion decisions"),

    # L3/L4: signals, candidates and settled outcomes. Small decision records
    # remain local indefinitely while full causal inputs age to cloud.
    _product("intraday_signal_events", "signals", "observed_at", ("model_version", "exchange_date", "signal_type"), local_tier="hot", local_hot_window_days=None, replay_role="decision_audit", description="material signal state transitions"),
    _product("intraday_signal_outcomes", "outcomes", "entry_observed_at+calculated_at", ("model_version", "calculated_year", "horizon"), local_tier="hot", local_hot_window_days=None, replay_role="forward_outcome_evaluation", description="settled intraday forward outcomes"),
    _product("strategy_watchlist_proposals", "signals", "as_of_date+created_at", ("strategy_key", "as_of_year"), local_tier="hot", local_hot_window_days=None, replay_role="universe_selection_replay", description="research-only watchlist proposals and review state"),
    _product("watchlist_main_wave_runs", "research", "strategy_available_at", ("model_version", "run_year"), local_tier="warm", local_hot_window_days=None, replay_role="negative_baseline_replay", description="deprecated main-wave experiment runs"),
    _product("watchlist_main_wave_candidates", "research", "strategy_available_at", ("model_version", "run_year", "symbol_bucket"), local_tier="warm", local_hot_window_days=None, replay_role="negative_baseline_replay", description="deprecated main-wave candidate evidence"),
    _product("watchlist_rebound_runs", "research", "strategy_available_at", ("model_version", "run_year"), local_tier="warm", local_hot_window_days=None, replay_role="countertrend_replay", description="countertrend rebound research runs"),
    _product("watchlist_rebound_candidates", "research", "strategy_available_at", ("model_version", "run_year", "symbol_bucket"), local_tier="warm", local_hot_window_days=None, replay_role="countertrend_replay", description="countertrend rebound candidates"),
    _product("ten_day_leader_rotation_runs", "research", "strategy_available_at", ("model_version", "run_year"), local_tier="hot", local_hot_window_days=None, replay_role="leader_pool_replay", description="ten-day leader universe and source status"),
    _product("ten_day_leader_rotation_candidates", "research", "discovered_at+run.strategy_available_at", ("model_version", "run_year", "board"), local_tier="hot", local_hot_window_days=None, replay_role="leader_pool_replay", description="ranked leader candidates"),
    _product("ten_day_leader_rotation_intraday_observations", "research", "observed_at", ("model_version", "exchange_date", "board", "hour"), local_tier="hot", local_hot_window_days=120, replay_role="leader_coordination_replay", description="leader/peer/VWAP coordination observations"),
    _product("post_close_strategy_runs", "research", "as_of_date+created_at", ("model_version", "run_year"), local_tier="hot", local_hot_window_days=None, replay_role="post_close_replay", description="post-close strategy run envelopes"),
    _product("post_close_strategy_candidates", "research", "discovered_at+run.created_at", ("model_version", "run_year", "symbol_bucket"), local_tier="hot", local_hot_window_days=None, replay_role="post_close_replay", description="ranked post-close candidates"),
    _product("post_close_strategy_screen_observations", "research", "run.as_of_date+created_at", ("model_version", "run_year", "screen_state", "symbol_bucket"), local_tier="hot", local_hot_window_days=None, replay_role="post_close_replay", description="full post-close screen population including rejected and insufficient-history controls"),
    _product("strategy_candidates", "research", "strategy_available_at", ("strategy_key", "model_version", "run_year"), local_tier="hot", local_hot_window_days=None, replay_role="candidate_comparison", description="logical cross-strategy candidate projection"),
    _product("strategy_pattern_runs", "research", "as_of_date+created_at", ("model_version", "run_year"), local_tier="warm", local_hot_window_days=None, replay_role="pattern_discovery_replay", description="bounded minute-pattern discovery runs"),
    _product("strategy_pattern_samples", "research", "run.as_of_date+run.created_at", ("model_version", "run_year", "cohort"), local_tier="warm", local_hot_window_days=None, replay_role="pattern_discovery_replay", description="positive and control pattern samples"),
    _product("xiaojie_leader_flow_observations", "research", "trading_date+first_seen_at", ("model_version", "exchange_date", "mode"), local_tier="hot", local_hot_window_days=365, replay_role="analyst_playbook_replay", description="point-in-time Xiaojie leader-flow classifications"),
    _product("strategy_experiments", "research", "created_at", ("strategy_key", "created_year"), local_tier="hot", local_hot_window_days=None, replay_role="walk_forward_trial_ledger", description="all tried variants and out-of-sample metrics"),
    _product("strategy_reviews", "outcomes", "exchange_date+observed_at", ("strategy_key", "review_year"), local_tier="hot", local_hot_window_days=None, replay_role="strategy_calibration_review", description="logical strategy review projection backed by strategy_review_runs"),
    _product("strategy_day_summaries", "outcomes", "exchange_date", ("exchange_year",), local_tier="hot", local_hot_window_days=None, replay_role="daily_strategy_attribution", description="daily cross-strategy summary"),

    # Analyst observations stay shadow inputs. Both source time and
    # strategy_available_at survive archival to prevent look-ahead.
    _product("analyst_observations", "features", "stated_at+strategy_available_at", ("analyst_id", "available_year", "subject_bucket"), local_tier="hot", local_hot_window_days=None, replay_role="analyst_point_in_time_replay", description="structured analyst claims with dual time boundary"),
    _product("analyst_evidence", "raw", "available_at", ("available_year", "evidence_type"), archive_format="jsonl_zstd", local_tier="warm", local_hot_window_days=None, replay_role="analyst_source_audit", description="content-addressed analyst evidence spans"),
    _product("analyst_market_reviews", "outcomes", "period_end", ("cadence", "review_year"), local_tier="hot", local_hot_window_days=None, replay_role="analyst_market_attribution", description="daily and weekly analyst versus market reviews"),

    # Operational receipts can never be market features or strategy inputs.
    _product("automation_runs", "control", "created_at", ("task_key", "created_year"), archive_format="jsonl_zstd", local_tier="hot", local_hot_window_days=None, replay_role="execution_audit_only", description="durable task execution receipts"),
    _product("retention_policies", "control", "updated_at", ("table_name",), archive_format="jsonl_zstd", local_tier="hot", local_hot_window_days=None, replay_role="execution_audit_only", description="per-table bounded-delete retention configuration and last-run receipts"),
)


DATA_PRODUCT_CONTRACTS: Final[dict[str, DataProductContract]] = {
    item.key: item for item in _PRODUCTS
}


def data_product_contract(key: str) -> DataProductContract:
    try:
        return DATA_PRODUCT_CONTRACTS[key]
    except KeyError as error:
        raise ValueError(f"unknown data product contract: {key}") from error


def data_product_contract_catalog() -> list[dict[str, Any]]:
    return [
        {
            "key": item.key,
            "layer": item.layer,
            "time_basis": item.time_basis,
            "partition_keys": list(item.partition_keys),
            "archive_format": item.archive_format,
            "cloud_retention": item.cloud_retention,
            "local_tier": item.local_tier,
            "local_hot_window_days": item.local_hot_window_days,
            "replay_role": item.replay_role,
            "description": item.description,
        }
        for item in sorted(DATA_PRODUCT_CONTRACTS.values(), key=lambda item: item.key)
    ]


def validate_declared_dataset_coverage(
    strategy_contracts: Iterable[Mapping[str, Any]],
    runtime_task_contracts: Iterable[Mapping[str, Any]],
) -> None:
    contracts = (*tuple(strategy_contracts), *tuple(runtime_task_contracts))
    declared = {
        str(dataset)
        for contract in contracts
        for dataset in contract.get("evidence_datasets", ())
    }
    missing = sorted(declared - set(DATA_PRODUCT_CONTRACTS))
    if missing:
        raise ValueError(f"declared evidence datasets missing data-product contracts: {', '.join(missing)}")


__all__ = [
    "DATA_PRODUCT_CONTRACTS", "DataProductContract", "data_product_contract",
    "data_product_contract_catalog", "validate_declared_dataset_coverage",
]
