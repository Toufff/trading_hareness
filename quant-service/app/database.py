from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Mapping

import psycopg
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row


SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS quant;

CREATE TABLE IF NOT EXISTS quant.instruments (
    symbol text PRIMARY KEY,
    exchange text NOT NULL,
    name text,
    industry text,
    list_date date,
    delist_date date,
    is_st boolean NOT NULL DEFAULT false,
    source text NOT NULL DEFAULT 'manual',
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS quant.market_bars_daily (
    symbol text NOT NULL REFERENCES quant.instruments(symbol),
    trading_date date NOT NULL,
    open numeric,
    high numeric,
    low numeric,
    close numeric NOT NULL,
    pre_close numeric,
    volume numeric,
    amount numeric,
    adj_factor numeric,
    is_suspended boolean NOT NULL DEFAULT false,
    limit_up numeric,
    limit_down numeric,
    source text NOT NULL,
    available_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, trading_date)
);
CREATE INDEX IF NOT EXISTS market_bars_daily_date_idx ON quant.market_bars_daily(trading_date DESC);

CREATE TABLE IF NOT EXISTS quant.market_events (
    event_id uuid PRIMARY KEY,
    symbol text REFERENCES quant.instruments(symbol),
    event_type text NOT NULL,
    occurred_at timestamptz NOT NULL,
    available_at timestamptz NOT NULL,
    source text NOT NULL,
    title text NOT NULL,
    body text,
    url text,
    content_sha256 text UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS market_events_symbol_time_idx ON quant.market_events(symbol, available_at DESC);

CREATE TABLE IF NOT EXISTS quant.analyst_signals (
    signal_id uuid PRIMARY KEY,
    ingestion_job_id uuid NOT NULL REFERENCES public.ingestion_jobs(job_id) ON DELETE CASCADE,
    analyst_id text NOT NULL,
    source_tag text NOT NULL,
    publisher_key text NOT NULL,
    symbol text NOT NULL REFERENCES quant.instruments(symbol),
    direction smallint NOT NULL CHECK (direction BETWEEN -1 AND 1),
    strength numeric NOT NULL CHECK (strength >= 0 AND strength <= 1),
    horizon_days integer NOT NULL CHECK (horizon_days IN (1, 5, 20, 60)),
    published_at timestamptz NOT NULL,
    available_at timestamptz NOT NULL,
    evidence_text text NOT NULL,
    evidence_offset integer,
    extraction_confidence numeric NOT NULL CHECK (extraction_confidence >= 0 AND extraction_confidence <= 1),
    extractor_version text NOT NULL,
    raw jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ingestion_job_id, symbol, horizon_days, extractor_version)
);
CREATE INDEX IF NOT EXISTS analyst_signals_symbol_time_idx ON quant.analyst_signals(symbol, available_at DESC);
CREATE INDEX IF NOT EXISTS analyst_signals_analyst_time_idx ON quant.analyst_signals(analyst_id, available_at DESC);

CREATE TABLE IF NOT EXISTS quant.analyst_scorecards (
    analyst_id text NOT NULL,
    horizon_days integer NOT NULL CHECK (horizon_days IN (1, 5, 20, 60)),
    as_of_date date NOT NULL,
    observations integer NOT NULL,
    hit_rate numeric,
    mean_excess_return numeric,
    mean_directional_return numeric,
    calibration_score numeric,
    methodology_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (analyst_id, horizon_days, as_of_date, methodology_version)
);

CREATE TABLE IF NOT EXISTS quant.recommendation_runs (
    run_id uuid PRIMARY KEY,
    as_of_date date NOT NULL,
    model_version text NOT NULL,
    market_regime text NOT NULL,
    source_status jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS quant.recommendations (
    run_id uuid NOT NULL REFERENCES quant.recommendation_runs(run_id) ON DELETE CASCADE,
    rank integer NOT NULL,
    symbol text NOT NULL REFERENCES quant.instruments(symbol),
    decision text NOT NULL CHECK (decision IN ('research_candidate', 'watch', 'no_trade')),
    score numeric NOT NULL,
    score_breakdown jsonb NOT NULL,
    explanation jsonb NOT NULL,
    risk_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (run_id, symbol),
    UNIQUE (run_id, rank)
);
"""


PLATFORM_SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS quant.providers (
    provider_key text PRIMARY KEY,
    label text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    config jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Provider/API contract observations are intentionally separate from generic
-- health: an HTTP response is not evidence that a capability is usable.
CREATE TABLE IF NOT EXISTS quant.provider_api_capabilities (
    provider_key text NOT NULL REFERENCES quant.providers(provider_key) ON DELETE CASCADE,
    api_name text NOT NULL,
    availability text NOT NULL CHECK (availability IN ('declared','verified','empty','unsupported','failed','unknown')),
    frequency text NOT NULL,
    decision_eligible boolean NOT NULL DEFAULT false,
    note text NOT NULL DEFAULT '',
    verified_at timestamptz,
    last_checked_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(provider_key, api_name)
);
CREATE INDEX IF NOT EXISTS provider_api_capabilities_status_idx
    ON quant.provider_api_capabilities(api_name, availability, last_checked_at DESC);

CREATE TABLE IF NOT EXISTS quant.provider_capabilities (
    provider_key text NOT NULL REFERENCES quant.providers(provider_key) ON DELETE CASCADE,
    capability text NOT NULL,
    market text NOT NULL DEFAULT 'cn',
    priority integer NOT NULL DEFAULT 100,
    enabled boolean NOT NULL DEFAULT true,
    rate_limit_per_minute integer,
    PRIMARY KEY (provider_key, capability, market)
);

CREATE TABLE IF NOT EXISTS quant.provider_health (
    provider_key text NOT NULL,
    capability text NOT NULL,
    market text NOT NULL DEFAULT 'cn',
    consecutive_failures integer NOT NULL DEFAULT 0,
    circuit_open_until timestamptz,
    last_success_at timestamptz,
    last_failure_at timestamptz,
    last_error text,
    last_latency_ms integer,
    last_row_count integer,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider_key, capability, market)
);

CREATE TABLE IF NOT EXISTS quant.fetch_runs (
    fetch_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_key text NOT NULL,
    capability text NOT NULL,
    market text NOT NULL DEFAULT 'cn',
    trade_date date,
    request_key text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('queued','running','completed','partial','failed','blocked')),
    attempt_count integer NOT NULL DEFAULT 0,
    row_count integer NOT NULL DEFAULT 0,
    started_at timestamptz,
    finished_at timestamptz,
    error_class text,
    error_message text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fetch_runs_status_idx ON quant.fetch_runs(status, created_at DESC);

CREATE TABLE IF NOT EXISTS quant.raw_market_observations (
    observation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_key text NOT NULL,
    capability text NOT NULL,
    market text NOT NULL DEFAULT 'cn',
    symbol text,
    effective_at timestamptz NOT NULL,
    available_at timestamptz NOT NULL,
    payload_sha256 text NOT NULL,
    normalized jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    fetch_run_id uuid REFERENCES quant.fetch_runs(fetch_run_id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(provider_key, capability, market, symbol, effective_at, payload_sha256)
);
CREATE INDEX IF NOT EXISTS raw_market_lookup_idx ON quant.raw_market_observations(capability, symbol, effective_at DESC);

-- Raw rows for the complete customer-enabled Tushare-compatible catalog.
-- Only selected APIs are promoted into canonical tables; all others remain
-- queryable, versioned evidence without forcing a premature schema choice.
CREATE TABLE IF NOT EXISTS quant.tushare_raw_records (
    record_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_key text NOT NULL DEFAULT 'tushare',
    api_name text NOT NULL,
    request_key text NOT NULL,
    record_index integer NOT NULL,
    record_key text NOT NULL,
    content_sha256 text NOT NULL,
    row_data jsonb NOT NULL,
    available_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(api_name, record_key, content_sha256)
);
ALTER TABLE quant.tushare_raw_records ADD COLUMN IF NOT EXISTS provider_key text NOT NULL DEFAULT 'tushare';
ALTER TABLE quant.tushare_raw_records DROP CONSTRAINT IF EXISTS tushare_raw_records_api_name_record_key_content_sha256_key;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='tushare_raw_records_provider_record_unique'
                   AND conrelid='quant.tushare_raw_records'::regclass) THEN
        ALTER TABLE quant.tushare_raw_records ADD CONSTRAINT tushare_raw_records_provider_record_unique
            UNIQUE(provider_key,api_name,record_key,content_sha256);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS tushare_raw_api_time_idx ON quant.tushare_raw_records(api_name, available_at DESC);
CREATE INDEX IF NOT EXISTS tushare_raw_provider_api_time_idx ON quant.tushare_raw_records(provider_key, api_name, available_at DESC);
CREATE INDEX IF NOT EXISTS tushare_raw_request_idx ON quant.tushare_raw_records(request_key, record_index);

CREATE TABLE IF NOT EXISTS quant.canonical_bars_daily (
    symbol text NOT NULL REFERENCES quant.instruments(symbol),
    trading_date date NOT NULL,
    open numeric,
    high numeric,
    low numeric,
    close numeric NOT NULL,
    pre_close numeric,
    volume numeric,
    amount numeric,
    adj_factor numeric,
    is_suspended boolean NOT NULL DEFAULT false,
    limit_up numeric,
    limit_down numeric,
    selected_provider text NOT NULL,
    source_observation_ids uuid[] NOT NULL DEFAULT '{}',
    quality_status text NOT NULL DEFAULT 'fresh' CHECK (quality_status IN ('fresh','partial','stale','conflicted','blocked')),
    available_at timestamptz NOT NULL,
    canonicalized_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, trading_date)
);
CREATE INDEX IF NOT EXISTS canonical_bars_date_idx ON quant.canonical_bars_daily(trading_date DESC);

CREATE TABLE IF NOT EXISTS quant.market_trade_calendar (
    exchange text NOT NULL,
    calendar_date date NOT NULL,
    is_open boolean NOT NULL,
    pretrade_date date,
    provider text NOT NULL DEFAULT 'tushare',
    available_at timestamptz NOT NULL,
    raw jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(exchange, calendar_date)
);

CREATE TABLE IF NOT EXISTS quant.daily_adjustment_factors (
    symbol text NOT NULL REFERENCES quant.instruments(symbol),
    trading_date date NOT NULL,
    adj_factor numeric NOT NULL,
    provider text NOT NULL DEFAULT 'tushare',
    available_at timestamptz NOT NULL,
    raw jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(symbol, trading_date, provider)
);

CREATE TABLE IF NOT EXISTS quant.daily_fundamentals (
    symbol text NOT NULL REFERENCES quant.instruments(symbol),
    trading_date date NOT NULL,
    close numeric,
    turnover_rate numeric,
    volume_ratio numeric,
    pe numeric,
    pb numeric,
    total_share numeric,
    float_share numeric,
    total_mv numeric,
    circ_mv numeric,
    provider text NOT NULL DEFAULT 'tushare',
    available_at timestamptz NOT NULL,
    raw jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(symbol, trading_date, provider)
);

CREATE TABLE IF NOT EXISTS quant.daily_trade_limits (
    symbol text NOT NULL REFERENCES quant.instruments(symbol),
    trading_date date NOT NULL,
    limit_up numeric,
    limit_down numeric,
    provider text NOT NULL DEFAULT 'tushare',
    available_at timestamptz NOT NULL,
    raw jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(symbol, trading_date, provider)
);

CREATE TABLE IF NOT EXISTS quant.security_suspensions (
    symbol text NOT NULL REFERENCES quant.instruments(symbol),
    suspend_date date NOT NULL,
    resume_date date,
    suspend_reason text,
    provider text NOT NULL DEFAULT 'tushare',
    available_at timestamptz NOT NULL,
    raw jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(symbol, suspend_date, provider)
);

CREATE TABLE IF NOT EXISTS quant.offline_imports (
    import_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_name text NOT NULL,
    file_name text NOT NULL,
    file_sha256 text NOT NULL UNIQUE,
    dataset_kind text NOT NULL CHECK (dataset_kind IN ('minute_bar')),
    status text NOT NULL CHECK (status IN ('running','completed','partial','failed')),
    row_count integer NOT NULL DEFAULT 0,
    rejected_rows integer NOT NULL DEFAULT 0,
    error_message text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE TABLE IF NOT EXISTS quant.market_bars_minute (
    symbol text NOT NULL REFERENCES quant.instruments(symbol),
    bar_time timestamptz NOT NULL,
    open numeric NOT NULL,
    high numeric NOT NULL,
    low numeric NOT NULL,
    close numeric NOT NULL,
    volume numeric,
    amount numeric,
    source_name text NOT NULL,
    import_id uuid NOT NULL REFERENCES quant.offline_imports(import_id) ON DELETE RESTRICT,
    available_at timestamptz NOT NULL,
    raw jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(symbol, bar_time, source_name)
);
CREATE INDEX IF NOT EXISTS market_bars_minute_time_idx ON quant.market_bars_minute(symbol, bar_time DESC);

-- Verified live-minute captures are deliberately separate from offline files.
-- They form a small per-watchlist, point-in-time baseline for time-of-day
-- volume surprise; they must never be confused with a market-wide minute feed.
CREATE TABLE IF NOT EXISTS quant.intraday_minute_sessions (
    symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE CASCADE,
    trading_date date NOT NULL,
    minute_bucket text NOT NULL CHECK (minute_bucket ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
    bar_time timestamptz NOT NULL,
    open numeric NOT NULL,
    high numeric NOT NULL,
    low numeric NOT NULL,
    close numeric NOT NULL,
    volume numeric,
    amount numeric,
    source_name text NOT NULL,
    available_at timestamptz NOT NULL,
    raw jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(symbol,trading_date,minute_bucket,source_name)
);
CREATE INDEX IF NOT EXISTS intraday_minute_sessions_profile_idx
    ON quant.intraday_minute_sessions(symbol,minute_bucket,trading_date DESC);

-- Intraday monitoring is deliberately separate from the research candidate
-- tables.  A watch entry is an explicit user scope, not an inferred order.
CREATE TABLE IF NOT EXISTS quant.intraday_watchlists (
    watchlist_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE CASCADE,
    label text,
    enabled boolean NOT NULL DEFAULT true,
    alert_on_entry boolean NOT NULL DEFAULT true,
    alert_on_exit boolean NOT NULL DEFAULT true,
    entry_price numeric,
    available_quantity integer NOT NULL DEFAULT 0 CHECK (available_quantity >= 0),
    hard_stop numeric,
    take_profit numeric,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(symbol),
    CHECK (entry_price IS NULL OR entry_price > 0),
    CHECK (hard_stop IS NULL OR hard_stop > 0),
    CHECK (take_profit IS NULL OR take_profit > 0)
);
CREATE INDEX IF NOT EXISTS intraday_watchlists_enabled_idx ON quant.intraday_watchlists(enabled, updated_at DESC);

CREATE TABLE IF NOT EXISTS quant.watchlist_factor_snapshots (
    factor_snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    watchlist_id uuid NOT NULL REFERENCES quant.intraday_watchlists(watchlist_id) ON DELETE CASCADE,
    symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE CASCADE,
    observed_at timestamptz NOT NULL,
    lookback_calendar_days integer NOT NULL,
    status text NOT NULL CHECK (status IN ('completed','partial','failed')),
    source_status jsonb NOT NULL DEFAULT '{}'::jsonb,
    factors jsonb NOT NULL DEFAULT '{}'::jsonb,
    model_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS watchlist_factor_snapshots_lookup_idx ON quant.watchlist_factor_snapshots(symbol, observed_at DESC);

CREATE TABLE IF NOT EXISTS quant.intraday_scan_runs (
    scan_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    observed_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('completed','partial','blocked','failed')),
    requested_symbols jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_status jsonb NOT NULL DEFAULT '{}'::jsonb,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS intraday_scan_runs_time_idx ON quant.intraday_scan_runs(observed_at DESC);

CREATE TABLE IF NOT EXISTS quant.intraday_quote_observations (
    quote_observation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scan_id uuid REFERENCES quant.intraday_scan_runs(scan_id) ON DELETE SET NULL,
    symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE CASCADE,
    observed_at timestamptz NOT NULL,
    source_name text NOT NULL,
    price numeric,
    pct_change numeric,
    volume_ratio numeric,
    turnover_rate numeric,
    main_net_inflow numeric,
    raw jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(symbol, source_name, observed_at)
);
CREATE INDEX IF NOT EXISTS intraday_quote_lookup_idx ON quant.intraday_quote_observations(symbol, observed_at DESC);
CREATE INDEX IF NOT EXISTS intraday_order_book_recent_idx
ON quant.intraday_quote_observations(symbol, observed_at DESC)
WHERE source_name='tencent_order_book';

CREATE TABLE IF NOT EXISTS quant.intraday_signal_events (
    signal_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scan_id uuid REFERENCES quant.intraday_scan_runs(scan_id) ON DELETE SET NULL,
    symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE CASCADE,
    signal_key text NOT NULL,
    signal_type text NOT NULL CHECK (signal_type IN ('entry','watch','reduce','exit','data_issue')),
    severity text NOT NULL CHECK (severity IN ('info','warning','critical')),
    state text NOT NULL CHECK (state IN ('detected','confirming','confirmed','alerted','invalidated','suppressed')),
    score numeric NOT NULL DEFAULT 0,
    observed_at timestamptz NOT NULL,
    expires_at timestamptz,
    conditions jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    risk_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS intraday_signal_key_time_idx ON quant.intraday_signal_events(signal_key, observed_at DESC);

-- A separate outcome ledger keeps post-signal measurements out of the signal
-- itself.  It is deliberately research-only: an outcome records what became
-- observable after an alert, never a fill or an order.
CREATE TABLE IF NOT EXISTS quant.intraday_signal_outcomes (
    signal_event_id uuid NOT NULL REFERENCES quant.intraday_signal_events(signal_event_id) ON DELETE CASCADE,
    horizon_key text NOT NULL CHECK (horizon_key IN ('5m','15m','30m','close','next_close')),
    direction smallint NOT NULL CHECK (direction IN (-1,1)),
    entry_observed_at timestamptz NOT NULL,
    entry_price numeric NOT NULL,
    exit_observed_at timestamptz,
    exit_price numeric,
    raw_return numeric,
    benchmark_return numeric,
    excess_return numeric,
    maximum_favorable_excursion numeric,
    maximum_adverse_excursion numeric,
    status text NOT NULL CHECK (status IN ('pending','matured','unavailable')),
    tradability text NOT NULL DEFAULT 'observed_quote_only',
    source_status jsonb NOT NULL DEFAULT '{}'::jsonb,
    calculated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(signal_event_id,horizon_key)
);
CREATE INDEX IF NOT EXISTS intraday_signal_outcomes_horizon_idx
    ON quant.intraday_signal_outcomes(horizon_key,status,calculated_at DESC);

CREATE TABLE IF NOT EXISTS quant.intraday_alert_deliveries (
    delivery_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_event_id uuid NOT NULL REFERENCES quant.intraday_signal_events(signal_event_id) ON DELETE CASCADE,
    channel text NOT NULL,
    status text NOT NULL CHECK (status IN ('disabled','pending','sent','failed','suppressed')),
    response jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_message text,
    sent_at timestamptz,
    message_text text,
    attempt_count integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS intraday_alert_delivery_idx ON quant.intraday_alert_deliveries(signal_event_id, created_at DESC);
CREATE INDEX IF NOT EXISTS intraday_alert_delivery_retry_idx
    ON quant.intraday_alert_deliveries(status, next_attempt_at, created_at)
    WHERE status IN ('pending','failed');

-- The primary Feishu adapter cannot report an outage while it is unavailable.
-- Keep the failure streak locally and send one recovery receipt on the first
-- later successful delivery, rather than generating noisy duplicate alerts.
CREATE TABLE IF NOT EXISTS quant.alert_delivery_health_events (
    health_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    channel text NOT NULL,
    event_type text NOT NULL CHECK (event_type IN ('failure_streak','recovered')),
    source_reference text NOT NULL,
    streak_count integer NOT NULL CHECK (streak_count >= 1),
    delivery_status text NOT NULL CHECK (delivery_status IN ('observed','pending','sent','failed','disabled')),
    message_text text NOT NULL DEFAULT '',
    response jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_message text,
    sent_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(channel,event_type,source_reference)
);
CREATE INDEX IF NOT EXISTS alert_delivery_health_events_status_idx
    ON quant.alert_delivery_health_events(channel, delivery_status, created_at DESC);

CREATE TABLE IF NOT EXISTS quant.intraday_board_reports (
    board_report_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    observed_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('completed','partial','blocked','failed')),
    source_status jsonb NOT NULL DEFAULT '{}'::jsonb,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS intraday_board_reports_time_idx ON quant.intraday_board_reports(observed_at DESC);

-- Lightweight one-minute board-flow snapshots back the live curve.  They are
-- deliberately separate from intraday_board_reports: the latter also carries
-- Tencent/member Top10 evidence and remains on its bounded strategy cadence.
CREATE TABLE IF NOT EXISTS quant.intraday_board_flow_snapshots (
    flow_snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_minute timestamptz NOT NULL UNIQUE,
    observed_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('completed','partial','failed')),
    coverage jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_status jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS intraday_board_flow_snapshots_time_idx
    ON quant.intraday_board_flow_snapshots(observed_at DESC);

CREATE TABLE IF NOT EXISTS quant.intraday_board_rotation_events (
    rotation_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_minute timestamptz NOT NULL,
    event_key text NOT NULL,
    taxonomy_key text NOT NULL,
    sector_key text NOT NULL,
    label text NOT NULL,
    event_type text NOT NULL CHECK (event_type IN ('cross_zero','flow_surge')),
    direction text NOT NULL CHECK (direction IN ('inflow','outflow')),
    state text NOT NULL CHECK (state IN ('confirming','confirmed','alerted','suppressed','expired')),
    first_observed_at timestamptz NOT NULL,
    last_observed_at timestamptz NOT NULL,
    confirmation_deadline timestamptz NOT NULL,
    conditions jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS intraday_board_rotation_event_key_idx
    ON quant.intraday_board_rotation_events(event_key, last_observed_at DESC);
CREATE INDEX IF NOT EXISTS intraday_board_rotation_state_idx
    ON quant.intraday_board_rotation_events(state, confirmation_deadline, last_observed_at DESC);

CREATE TABLE IF NOT EXISTS quant.intraday_board_rotation_deliveries (
    delivery_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rotation_event_id uuid NOT NULL REFERENCES quant.intraday_board_rotation_events(rotation_event_id) ON DELETE CASCADE,
    channel text NOT NULL,
    status text NOT NULL CHECK (status IN ('disabled','pending','sent','failed','suppressed')),
    response jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_message text,
    message_text text NOT NULL DEFAULT '',
    attempt_count integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz,
    sent_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(rotation_event_id, channel)
);
CREATE INDEX IF NOT EXISTS intraday_board_rotation_delivery_retry_idx
    ON quant.intraday_board_rotation_deliveries(status, next_attempt_at, created_at)
    WHERE status IN ('pending','failed');

CREATE TABLE IF NOT EXISTS quant.intraday_board_report_deliveries (
    delivery_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    board_report_id uuid NOT NULL REFERENCES quant.intraday_board_reports(board_report_id) ON DELETE CASCADE,
    channel text NOT NULL,
    status text NOT NULL CHECK (status IN ('disabled','sent','failed')),
    response jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_message text,
    sent_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS intraday_board_report_delivery_idx ON quant.intraday_board_report_deliveries(board_report_id, created_at DESC);

-- Whole-market snapshot runs describe exactly what was observed at the
-- midday and close checkpoints. They are not recommendations by themselves.
CREATE TABLE IF NOT EXISTS quant.market_snapshot_runs (
    market_snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_key text NOT NULL UNIQUE,
    session text NOT NULL CHECK (session IN ('midday','close')),
    exchange_date date NOT NULL,
    observed_at timestamptz NOT NULL,
    universe_key text NOT NULL,
    universe_count integer NOT NULL,
    quote_count integer NOT NULL,
    coverage numeric NOT NULL,
    status text NOT NULL CHECK (status IN ('ready','degraded','blocked','failed')),
    decision_eligible boolean NOT NULL DEFAULT false,
    source_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    quality_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS market_snapshot_runs_time_idx
    ON quant.market_snapshot_runs(exchange_date DESC, session, observed_at DESC);

-- Sector memberships are point-in-time and many-to-many. A concept board is
-- not forced into an industry taxonomy, which keeps source semantics intact.
CREATE TABLE IF NOT EXISTS quant.sector_taxonomies (
    taxonomy_key text PRIMARY KEY,
    label text NOT NULL,
    provider_key text NOT NULL REFERENCES quant.providers(provider_key) ON DELETE RESTRICT,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS quant.sectors (
    taxonomy_key text NOT NULL REFERENCES quant.sector_taxonomies(taxonomy_key) ON DELETE CASCADE,
    sector_key text NOT NULL,
    label text NOT NULL,
    parent_sector_key text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(taxonomy_key, sector_key)
);

CREATE TABLE IF NOT EXISTS quant.sector_membership_history (
    taxonomy_key text NOT NULL,
    sector_key text NOT NULL,
    symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE CASCADE,
    effective_from date NOT NULL,
    effective_to date,
    provider_key text NOT NULL REFERENCES quant.providers(provider_key) ON DELETE RESTRICT,
    available_at timestamptz NOT NULL,
    raw jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(taxonomy_key, sector_key, symbol, effective_from),
    FOREIGN KEY(taxonomy_key, sector_key) REFERENCES quant.sectors(taxonomy_key, sector_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS sector_membership_symbol_time_idx
    ON quant.sector_membership_history(symbol, effective_from DESC);

-- A durable cursor for the bounded THS concept-member backfill.  It prevents
-- a restart or a transient supplier error from making a partially hydrated
-- board universe look complete.
CREATE TABLE IF NOT EXISTS quant.sector_member_sync_state (
    taxonomy_key text NOT NULL,
    sector_key text NOT NULL,
    trading_date date NOT NULL,
    state text NOT NULL CHECK (state IN ('completed','empty','failed')),
    attempts integer NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    member_count integer NOT NULL DEFAULT 0 CHECK (member_count >= 0),
    last_error text,
    provider_key text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(taxonomy_key, sector_key, trading_date),
    FOREIGN KEY(taxonomy_key, sector_key) REFERENCES quant.sectors(taxonomy_key, sector_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS sector_member_sync_state_resume_idx
    ON quant.sector_member_sync_state(taxonomy_key, trading_date DESC, state, updated_at);

-- Daily sector observations retain the provider's exact board semantics.
-- They are deliberately separate from stock-level moneyflow features.
CREATE TABLE IF NOT EXISTS quant.sector_market_observations (
    taxonomy_key text NOT NULL,
    sector_key text NOT NULL,
    trading_date date NOT NULL,
    provider_key text NOT NULL REFERENCES quant.providers(provider_key) ON DELETE RESTRICT,
    available_at timestamptz NOT NULL,
    close numeric,
    change_pct numeric,
    net_amount numeric,
    net_buy_amount numeric,
    net_sell_amount numeric,
    constituent_count integer,
    leading_symbol text,
    leading_label text,
    raw jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(taxonomy_key, sector_key, trading_date, provider_key),
    FOREIGN KEY(taxonomy_key, sector_key) REFERENCES quant.sectors(taxonomy_key, sector_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS sector_market_observations_date_idx
    ON quant.sector_market_observations(taxonomy_key, trading_date DESC, net_amount DESC);

-- Candidate rows retain the exact constituent and limit-up evidence used to
-- connect a funded board to its limit-up stocks.
CREATE TABLE IF NOT EXISTS quant.sector_limit_candidates (
    taxonomy_key text NOT NULL,
    sector_key text NOT NULL,
    symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE CASCADE,
    trading_date date NOT NULL,
    provider_key text NOT NULL REFERENCES quant.providers(provider_key) ON DELETE RESTRICT,
    available_at timestamptz NOT NULL,
    name text,
    limit_tag text,
    limit_type text,
    pct_change numeric,
    price numeric,
    limit_amount numeric,
    turnover_rate numeric,
    open_num numeric,
    status text,
    description text,
    raw jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (taxonomy_key, sector_key, symbol, trading_date, provider_key),
    FOREIGN KEY(taxonomy_key, sector_key) REFERENCES quant.sectors(taxonomy_key, sector_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS sector_limit_candidates_date_idx
    ON quant.sector_limit_candidates(trading_date DESC, taxonomy_key, sector_key, limit_amount DESC);

CREATE TABLE IF NOT EXISTS quant.data_quality_issues (
    issue_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_key text,
    capability text NOT NULL,
    symbol text,
    trading_date date,
    severity text NOT NULL CHECK (severity IN ('info','warning','error','blocking')),
    code text NOT NULL,
    message text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    resolved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS data_quality_open_idx ON quant.data_quality_issues(severity, created_at DESC) WHERE resolved_at IS NULL;

CREATE TABLE IF NOT EXISTS quant.data_snapshots (
    snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_key text NOT NULL UNIQUE,
    as_of_date date NOT NULL,
    knowledge_cutoff timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('building','ready','blocked')),
    manifest jsonb NOT NULL DEFAULT '{}'::jsonb,
    content_sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    finalized_at timestamptz
);

CREATE TABLE IF NOT EXISTS quant.remote_analysts (
    remote_analyst_id text PRIMARY KEY,
    name text NOT NULL,
    organization text NOT NULL DEFAULT '',
    description text NOT NULL DEFAULT '',
    remote_updated_at timestamptz,
    remote_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    synced_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS quant.remote_reports (
    remote_report_id text PRIMARY KEY,
    remote_analyst_id text NOT NULL REFERENCES quant.remote_analysts(remote_analyst_id),
    report_date date NOT NULL,
    title text NOT NULL,
    summary text NOT NULL DEFAULT '',
    source_url text,
    remote_version text NOT NULL,
    content_hash text NOT NULL,
    remote_created_at timestamptz,
    remote_updated_at timestamptz,
    raw_markdown text NOT NULL DEFAULT '',
    sections jsonb NOT NULL DEFAULT '{}'::jsonb,
    materials jsonb NOT NULL DEFAULT '[]'::jsonb,
    mentioned_stocks jsonb NOT NULL DEFAULT '[]'::jsonb,
    mentioned_sectors jsonb NOT NULL DEFAULT '[]'::jsonb,
    predictions jsonb NOT NULL DEFAULT '[]'::jsonb,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    first_synced_at timestamptz NOT NULL DEFAULT now(),
    synced_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(remote_analyst_id, report_date, remote_version, content_hash)
);
CREATE INDEX IF NOT EXISTS remote_reports_analyst_date_idx ON quant.remote_reports(remote_analyst_id, report_date DESC);

-- The current row is deliberately small and fast to query.  Every distinct
-- remote version is also retained below, so a correction made by the archive
-- never overwrites the evidence that an earlier research run used.
CREATE TABLE IF NOT EXISTS quant.remote_report_versions (
    remote_report_id text NOT NULL REFERENCES quant.remote_reports(remote_report_id) ON DELETE CASCADE,
    remote_version text NOT NULL,
    content_hash text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (remote_report_id, remote_version, content_hash)
);
CREATE INDEX IF NOT EXISTS remote_report_versions_seen_idx ON quant.remote_report_versions(last_seen_at DESC);

CREATE TABLE IF NOT EXISTS quant.analyst_evidence (
    evidence_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    remote_report_id text NOT NULL REFERENCES quant.remote_reports(remote_report_id) ON DELETE CASCADE,
    evidence_key text NOT NULL,
    evidence_type text NOT NULL,
    body text NOT NULL,
    location jsonb NOT NULL DEFAULT '{}'::jsonb,
    content_sha256 text NOT NULL,
    available_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(remote_report_id, evidence_key, content_sha256)
);
CREATE INDEX IF NOT EXISTS analyst_evidence_report_idx ON quant.analyst_evidence(remote_report_id);

CREATE TABLE IF NOT EXISTS quant.analyst_claims (
    claim_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    evidence_id uuid NOT NULL REFERENCES quant.analyst_evidence(evidence_id) ON DELETE CASCADE,
    remote_analyst_id text NOT NULL REFERENCES quant.remote_analysts(remote_analyst_id),
    scope text NOT NULL CHECK (scope IN ('market','sector','theme','stock')),
    subject_key text NOT NULL,
    subject_label text,
    direction smallint NOT NULL CHECK (direction BETWEEN -1 AND 1),
    strength numeric NOT NULL CHECK (strength >= 0 AND strength <= 1),
    horizon_days integer NOT NULL DEFAULT 20,
    conditions jsonb NOT NULL DEFAULT '[]'::jsonb,
    invalidation jsonb NOT NULL DEFAULT '[]'::jsonb,
    extraction_confidence numeric NOT NULL CHECK (extraction_confidence >= 0 AND extraction_confidence <= 1),
    extractor_version text NOT NULL,
    available_at timestamptz NOT NULL,
    raw jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(evidence_id, scope, subject_key, horizon_days, extractor_version)
);
CREATE INDEX IF NOT EXISTS analyst_claims_subject_idx ON quant.analyst_claims(subject_key, available_at DESC);

-- A point-in-time journal for the noon/close decision loop.  The report is
-- deliberately evidence-only: it records what was known at that checkpoint,
-- never an order or a later rewritten explanation.
CREATE TABLE IF NOT EXISTS quant.strategy_review_runs (
    review_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    review_key text NOT NULL UNIQUE,
    exchange_date date NOT NULL,
    session text NOT NULL CHECK (session IN ('midday','close')),
    observed_at timestamptz NOT NULL,
    market_state text NOT NULL,
    data_boundary jsonb NOT NULL DEFAULT '{}'::jsonb,
    report jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS strategy_review_runs_time_idx
    ON quant.strategy_review_runs(exchange_date DESC, session, observed_at DESC);

-- One bounded, auditable end-of-day message.  It references already persisted
-- evidence and is not a second market-data store.
CREATE TABLE IF NOT EXISTS quant.strategy_day_summaries (
    summary_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    exchange_date date NOT NULL UNIQUE,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    message_text text NOT NULL DEFAULT '',
    delivery_status text NOT NULL DEFAULT 'pending' CHECK (delivery_status IN ('pending','sent','failed','disabled','suppressed')),
    attempt_count integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz,
    sent_at timestamptz,
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS strategy_day_summaries_delivery_idx
    ON quant.strategy_day_summaries(delivery_status, next_attempt_at, exchange_date DESC);

-- A compact, reproducible post-close universe screen.  It intentionally keeps
-- only ranked evidence, not a second copy of every daily bar or a broad minute
-- archive.  Candidates are research observations for the next session.
CREATE TABLE IF NOT EXISTS quant.post_close_strategy_runs (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_key text NOT NULL UNIQUE,
    as_of_date date NOT NULL,
    model_version text NOT NULL,
    status text NOT NULL CHECK (status IN ('completed','partial','blocked')),
    source_status jsonb NOT NULL DEFAULT '{}'::jsonb,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS post_close_strategy_runs_date_idx
    ON quant.post_close_strategy_runs(as_of_date DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS quant.post_close_strategy_candidates (
    run_id uuid NOT NULL REFERENCES quant.post_close_strategy_runs(run_id) ON DELETE CASCADE,
    rank integer NOT NULL,
    symbol text NOT NULL REFERENCES quant.instruments(symbol),
    candidate_type text NOT NULL CHECK (candidate_type IN ('base_ready_30d','base_forming_15d','fresh_start_15d')),
    score numeric NOT NULL,
    structure jsonb NOT NULL DEFAULT '{}'::jsonb,
    board_context jsonb NOT NULL DEFAULT '{}'::jsonb,
    risk_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY(run_id,symbol),
    UNIQUE(run_id,rank)
);
CREATE INDEX IF NOT EXISTS post_close_strategy_candidates_rank_idx
    ON quant.post_close_strategy_candidates(run_id,rank);

-- A bounded post-close replay set for limit-up leaders, first boards,
-- consecutive boards and deep intraday reversals.  The table stores compact
-- derived evidence and a capped minute curve for selected samples only; it is
-- not a second full-market minute database.
CREATE TABLE IF NOT EXISTS quant.strategy_pattern_runs (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_key text NOT NULL UNIQUE,
    as_of_date date NOT NULL,
    model_version text NOT NULL,
    status text NOT NULL CHECK (status IN ('completed','partial','blocked')),
    source_status jsonb NOT NULL DEFAULT '{}'::jsonb,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS strategy_pattern_runs_date_idx
    ON quant.strategy_pattern_runs(as_of_date DESC,updated_at DESC);

CREATE TABLE IF NOT EXISTS quant.strategy_pattern_samples (
    run_id uuid NOT NULL REFERENCES quant.strategy_pattern_runs(run_id) ON DELETE CASCADE,
    rank integer NOT NULL,
    symbol text NOT NULL REFERENCES quant.instruments(symbol),
    name text,
    primary_cohort text NOT NULL,
    cohorts jsonb NOT NULL DEFAULT '[]'::jsonb,
    board_context jsonb NOT NULL DEFAULT '{}'::jsonb,
    limit_context jsonb NOT NULL DEFAULT '{}'::jsonb,
    daily_features jsonb NOT NULL DEFAULT '{}'::jsonb,
    intraday_pattern jsonb NOT NULL DEFAULT '{}'::jsonb,
    minute_source text,
    risk_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY(run_id,symbol),
    UNIQUE(run_id,rank)
);
CREATE INDEX IF NOT EXISTS strategy_pattern_samples_rank_idx
    ON quant.strategy_pattern_samples(run_id,rank);

CREATE TABLE IF NOT EXISTS quant.claim_revisions (
    revision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    prior_claim_id uuid REFERENCES quant.analyst_claims(claim_id) ON DELETE SET NULL,
    claim_id uuid NOT NULL REFERENCES quant.analyst_claims(claim_id) ON DELETE CASCADE,
    revision_type text NOT NULL CHECK (revision_type IN ('new','confirm','weaken','reverse','withdraw')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(claim_id, revision_type)
);

CREATE TABLE IF NOT EXISTS quant.outcomes (
    outcome_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id uuid REFERENCES quant.analyst_claims(claim_id) ON DELETE CASCADE,
    recommendation_run_id uuid,
    symbol text NOT NULL REFERENCES quant.instruments(symbol),
    entry_date date NOT NULL,
    horizon_days integer NOT NULL,
    entry_close numeric NOT NULL,
    exit_close numeric,
    raw_return numeric,
    benchmark_return numeric,
    excess_return numeric,
    maximum_favorable_excursion numeric,
    maximum_adverse_excursion numeric,
    tradability text NOT NULL DEFAULT 'unknown',
    calculated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(claim_id, symbol, entry_date, horizon_days)
);

CREATE TABLE IF NOT EXISTS quant.experiments (
    experiment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    snapshot_id uuid REFERENCES quant.data_snapshots(snapshot_id) ON DELETE RESTRICT,
    code_version text NOT NULL,
    parameters jsonb NOT NULL DEFAULT '{}'::jsonb,
    metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL CHECK (status IN ('queued','running','completed','failed')),
    artifact_uri text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

-- A controlled candidate universe keeps market-wide scans explicit.  Analysts
-- may add evidence for any symbol, but only enabled universe members become
-- quant-led candidates without an explicit analyst claim.
CREATE TABLE IF NOT EXISTS quant.universe_members (
    universe_key text NOT NULL DEFAULT 'core',
    symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE CASCADE,
    enabled boolean NOT NULL DEFAULT true,
    priority integer NOT NULL DEFAULT 100,
    source text NOT NULL DEFAULT 'manual',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    added_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(universe_key, symbol)
);
CREATE INDEX IF NOT EXISTS universe_members_active_idx ON quant.universe_members(universe_key, priority, symbol) WHERE enabled;

-- Features are immutable for a specific data cutoff.  A scoring run reads one
-- version instead of recomputing against whatever provider data arrived later.
CREATE TABLE IF NOT EXISTS quant.feature_snapshots (
    feature_snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_key text NOT NULL,
    symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE CASCADE,
    as_of_date date NOT NULL,
    feature_version text NOT NULL,
    features jsonb NOT NULL,
    quality_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(snapshot_key, symbol, feature_version)
);
CREATE INDEX IF NOT EXISTS feature_snapshots_symbol_date_idx ON quant.feature_snapshots(symbol, as_of_date DESC);

-- Ambiguous report text is retained as a review item rather than silently
-- inventing a stock-level analyst claim.
CREATE TABLE IF NOT EXISTS quant.claim_review_queue (
    review_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    evidence_id uuid NOT NULL REFERENCES quant.analyst_evidence(evidence_id) ON DELETE CASCADE,
    suggested_scope text NOT NULL,
    suggested_label text NOT NULL,
    suggested_symbol text,
    direction smallint NOT NULL CHECK (direction BETWEEN -1 AND 1),
    strength numeric NOT NULL CHECK (strength >= 0 AND strength <= 1),
    horizon_days integer NOT NULL,
    extraction_confidence numeric NOT NULL CHECK (extraction_confidence >= 0 AND extraction_confidence <= 1),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    reviewed_at timestamptz,
    reviewer_note text,
    raw jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(evidence_id, suggested_scope, suggested_label)
);
CREATE INDEX IF NOT EXISTS claim_review_pending_idx ON quant.claim_review_queue(status, created_at DESC) WHERE status='pending';

-- Factor definitions are declarative.  A framework adapter may consume the
-- same definition, but production recommendations use only evaluated factors.
CREATE TABLE IF NOT EXISTS quant.factor_registry (
    factor_key text PRIMARY KEY,
    label text NOT NULL,
    category text NOT NULL,
    implementation text NOT NULL,
    inputs jsonb NOT NULL DEFAULT '[]'::jsonb,
    formula jsonb NOT NULL DEFAULT '{}'::jsonb,
    framework_tags jsonb NOT NULL DEFAULT '[]'::jsonb,
    version text NOT NULL,
    status text NOT NULL CHECK (status IN ('experimental','active','disabled')),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS quant.factor_evaluations (
    evaluation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    factor_key text NOT NULL REFERENCES quant.factor_registry(factor_key),
    universe_key text NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    horizon_days integer NOT NULL CHECK (horizon_days IN (1,5,20,60)),
    engine text NOT NULL,
    status text NOT NULL CHECK (status IN ('completed','insufficient_history','failed')),
    observations integer NOT NULL DEFAULT 0,
    cross_section_days integer NOT NULL DEFAULT 0,
    metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
    artifact jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS factor_evaluations_factor_time_idx ON quant.factor_evaluations(factor_key, created_at DESC);

CREATE TABLE IF NOT EXISTS quant.strategy_registry (
    strategy_key text PRIMARY KEY,
    label text NOT NULL,
    engine text NOT NULL,
    version text NOT NULL,
    configuration jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL CHECK (status IN ('experimental','active','disabled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS quant.strategy_experiments (
    strategy_experiment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_key text NOT NULL REFERENCES quant.strategy_registry(strategy_key),
    universe_key text NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    status text NOT NULL CHECK (status IN ('completed','insufficient_history','failed')),
    parameters jsonb NOT NULL DEFAULT '{}'::jsonb,
    metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
    equity_curve jsonb NOT NULL DEFAULT '[]'::jsonb,
    trades jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS strategy_experiments_strategy_time_idx ON quant.strategy_experiments(strategy_key, created_at DESC);

CREATE TABLE IF NOT EXISTS quant.research_frameworks (
    framework_key text PRIMARY KEY,
    label text NOT NULL,
    role text NOT NULL,
    integration_mode text NOT NULL,
    status text NOT NULL CHECK (status IN ('native','adapter_ready','planned','disabled')),
    license_note text NOT NULL,
    prerequisites jsonb NOT NULL DEFAULT '[]'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE quant.recommendation_runs ADD COLUMN IF NOT EXISTS snapshot_key text;
ALTER TABLE quant.recommendation_runs ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'completed';
ALTER TABLE quant.recommendation_runs ADD COLUMN IF NOT EXISTS model_metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE quant.recommendations ADD COLUMN IF NOT EXISTS direction smallint NOT NULL DEFAULT 0;
ALTER TABLE quant.recommendations ADD COLUMN IF NOT EXISTS horizon_days integer NOT NULL DEFAULT 20;
ALTER TABLE quant.recommendations ADD COLUMN IF NOT EXISTS confidence numeric NOT NULL DEFAULT 0;
ALTER TABLE quant.recommendations ADD COLUMN IF NOT EXISTS valid_until date;
ALTER TABLE quant.recommendations ADD COLUMN IF NOT EXISTS invalidation jsonb NOT NULL DEFAULT '[]'::jsonb;
CREATE INDEX IF NOT EXISTS recommendations_symbol_idx ON quant.recommendations(symbol, run_id);
ALTER TABLE quant.outcomes ADD COLUMN IF NOT EXISTS direction smallint NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS recommendation_outcomes_unique_idx
    ON quant.outcomes(recommendation_run_id, symbol, entry_date, horizon_days)
    WHERE recommendation_run_id IS NOT NULL;

INSERT INTO quant.providers(provider_key,label) VALUES
    ('tushare','Tushare（历史）'), ('tushare_primary','Tushare 兼容主源'),
    ('tushare_super','Tushare 超级路径源（历史聚合身份）'),
    ('tushare_super_sdk','Tushare 超级 SDK 代理源'),
    ('tushare_super_get','Tushare 超级 GET 网关'),
    ('tushare_backup','Tushare REST 备用源'),
    ('akshare','AKShare'), ('baostock','BaoStock'),
    ('eastmoney_free','东方财富公开行情'), ('tencent_free','腾讯财经公开日线'), ('sina_free','新浪财经公开报价'),
    ('cninfo_free','巨潮资讯公开公告'),
    ('remote_archive','远端市场复盘档案 API')
ON CONFLICT(provider_key) DO NOTHING;

UPDATE quant.providers SET enabled=false,updated_at=now()
 WHERE provider_key='tushare_super';
UPDATE quant.providers SET label='Tushare 超级路径源（历史聚合身份）',updated_at=now()
 WHERE provider_key='tushare_super';

INSERT INTO quant.provider_api_capabilities(provider_key,api_name,availability,frequency,decision_eligible,note,verified_at,metadata) VALUES
    ('tushare_primary','daily','verified','daily_or_auction',true,'Primary daily endpoint returned market rows during audit.',now(),'{}'),
    ('tushare_primary','stock_basic','verified','reference_daily',true,'Primary reference endpoint is enabled. ',now(),'{}'),
    ('tushare_primary','stk_auction_o','verified','daily_or_auction',true,'Opening auction returned rows during audit.',now(),'{}'),
    ('tushare_primary','stk_auction_c','verified','daily_or_auction',true,'Closing auction returned rows during audit.',now(),'{}'),
    ('tushare_primary','rt_min','declared','realtime',false,'Only probe during an SSE trading session; awaiting a live main-route verification.',null,'{"session_restriction":"SSE continuous auction only"}'),
    ('tushare_super','daily','verified','daily_or_auction',true,'Super SDK daily endpoint returned rows during audit.',now(),'{}'),
    ('tushare_super','cyq_perf','verified','daily_or_event',true,'Super SDK chip performance endpoint returned rows during audit.',now(),'{}'),
    ('tushare_super','cyq_chips','verified','daily_or_event',true,'Super SDK chip distribution endpoint returned rows during audit.',now(),'{}'),
    ('tushare_super','moneyflow','verified','daily_or_event',true,'Super SDK stock moneyflow endpoint returned rows during audit.',now(),'{}'),
    ('tushare_super','stk_factor_pro','verified','daily_or_event',true,'Super SDK professional factor endpoint returned rows during audit.',now(),'{}'),
    ('tushare_super','rt_min','declared','realtime',false,'Only probe during an SSE trading session; awaiting a live SDK-route verification.',null,'{"session_restriction":"SSE continuous auction only"}')
ON CONFLICT(provider_key,api_name) DO NOTHING;

-- Preserve the historical SDK audit under its physical provider identity.
-- The old aggregate rows remain immutable evidence but are disabled for new
-- routing so GET and SDK observations can no longer overwrite each other.
INSERT INTO quant.provider_api_capabilities(
    provider_key,api_name,availability,frequency,decision_eligible,note,
    verified_at,last_checked_at,metadata
)
SELECT 'tushare_super_sdk',api_name,availability,frequency,decision_eligible,
       CASE WHEN note='' THEN 'Migrated from the historical super SDK audit.' ELSE note END,
       verified_at,last_checked_at,metadata || '{"identity_migrated_from":"tushare_super"}'::jsonb
  FROM quant.provider_api_capabilities
 WHERE provider_key='tushare_super' AND api_name NOT IN ('rt_k','rt_min','rt_min_daily')
ON CONFLICT(provider_key,api_name) DO NOTHING;

INSERT INTO quant.provider_api_capabilities(provider_key,api_name,availability,frequency,decision_eligible,note,verified_at,metadata) VALUES
    ('tushare_super_get','daily','verified','daily_or_auction',true,'GET gateway returned current equity daily rows.',now(),'{"protocol":"get_x_api_key","completeness":"bounded_query_verified"}'),
    ('tushare_super_get','daily_basic','verified','daily_or_auction',true,'GET gateway returned daily-basic rows.',now(),'{"protocol":"get_x_api_key"}'),
    ('tushare_super_get','index_daily','verified','daily_or_auction',true,'GET gateway returned index daily rows.',now(),'{"protocol":"get_x_api_key"}'),
    ('tushare_super_get','stock_basic','verified','reference_daily',true,'GET gateway returned stock reference rows.',now(),'{"protocol":"get_x_api_key"}'),
    ('tushare_super_get','cyq_perf','verified','daily_or_event',true,'GET gateway returned chip-performance rows.',now(),'{"protocol":"get_x_api_key"}'),
    ('tushare_super_get','moneyflow','verified','daily_or_event',true,'GET gateway returned stock moneyflow rows.',now(),'{"protocol":"get_x_api_key"}'),
    ('tushare_super_get','moneyflow_cnt_ths','verified','daily_or_event',false,'GET gateway returned the THS concept-flow cross-section.',now(),'{"protocol":"get_x_api_key"}'),
    ('tushare_super_get','ths_member','verified','daily_or_event',false,'GET gateway returned exact THS constituent rows.',now(),'{"protocol":"get_x_api_key","completeness":"requires_terminal_page"}'),
    ('tushare_super_get','top_list','verified','daily_or_event',false,'GET gateway returned Dragon-Tiger List rows.',now(),'{"protocol":"get_x_api_key"}'),
    ('tushare_super_get','fut_basic','verified','reference_periodic',false,'GET gateway returned futures reference rows.',now(),'{"protocol":"get_x_api_key"}'),
    ('tushare_super_get','cn_gdp','empty','quarterly',false,'GET gateway returned a valid empty response for the requested current quarter.',null,'{"protocol":"get_x_api_key"}'),
    ('tushare_super_get','rt_k','verified','realtime',false,'GET gateway returned a current equity quote during continuous auction.',now(),'{"protocol":"get_x_api_key","session_restriction":"SSE continuous auction only"}'),
    ('tushare_super_get','rt_min','verified','realtime',false,'GET gateway returned current equity minute rows during continuous auction.',now(),'{"protocol":"get_x_api_key","session_restriction":"SSE continuous auction only"}'),
    ('tushare_super_get','rt_min_daily','verified','realtime',false,'GET gateway returned the current full-session equity minute series.',now(),'{"protocol":"get_x_api_key","session_restriction":"SSE continuous auction only","completeness":"bounded_session_verified"}')
ON CONFLICT(provider_key,api_name) DO NOTHING;

UPDATE quant.provider_api_capabilities
   SET note='GET route is verified for bounded THS member queries but caps broad boards and ignores offset; use super SDK for complete single-board snapshots.',
       metadata=metadata || '{"completeness":"bounded_only","pagination":"offset_ignored"}'::jsonb,
       last_checked_at=now()
 WHERE provider_key='tushare_super_get' AND api_name='ths_member';

UPDATE quant.provider_api_capabilities
   SET note='SDK route returns the complete single-board THS member response; duplicate gateway layouts are normalized and deduplicated by con_code.',
       metadata=metadata || '{"completeness":"single_board_complete","normalization":"con_code_deduplicated"}'::jsonb,
       last_checked_at=now()
 WHERE provider_key='tushare_super_sdk' AND api_name='ths_member';

INSERT INTO quant.provider_api_capabilities(provider_key,api_name,availability,frequency,decision_eligible,note,verified_at,metadata)
SELECT provider_key,api_name,'unsupported','realtime',false,
       'This physical route has no verified realtime capability and is excluded from realtime decisions.',
       null,'{"realtime_route":false}'::jsonb
  FROM (VALUES
        ('tushare_primary','rt_k'),('tushare_primary','rt_min'),('tushare_primary','rt_min_daily'),
        ('tushare_super_sdk','rt_k'),('tushare_super_sdk','rt_min'),('tushare_super_sdk','rt_min_daily')
  ) AS denied(provider_key,api_name)
ON CONFLICT(provider_key,api_name) DO UPDATE SET
    availability='unsupported',decision_eligible=false,note=EXCLUDED.note,
    verified_at=null,last_checked_at=now(),metadata=quant.provider_api_capabilities.metadata || EXCLUDED.metadata;

UPDATE quant.provider_api_capabilities
   SET availability='declared', decision_eligible=false,
       note='Only probe during an SSE trading session; prior off-session responses are not conclusive.',
       verified_at=null, last_checked_at=now()
 WHERE provider_key='tushare_super' AND api_name='rt_min' AND availability='unsupported';

INSERT INTO quant.provider_capabilities(provider_key,capability,market,priority,enabled,rate_limit_per_minute) VALUES
    ('tushare_primary','daily_bar','cn',10,true,60),
    ('tushare_primary','daily','cn',10,true,60),
    ('tushare_primary','stock_basic','cn',10,true,60),
    ('tushare_primary','rt_min','cn',10,false,10),
    ('tushare_super','daily_bar','cn',20,false,60),
    ('tushare_super','realtime_quote','cn',20,false,10),
    ('tushare_super','rt_min','cn',20,false,10),
    ('tushare_super','stock_basic','cn',20,false,30),
    ('tushare_super_sdk','daily_bar','cn',20,true,30),
    ('tushare_super_sdk','specialty','cn',20,true,30),
    ('tushare_super_sdk','stock_basic','cn',20,true,30),
    ('tushare_super_get','daily_bar','cn',15,true,60),
    ('tushare_super_get','realtime_quote','cn',15,true,60),
    ('tushare_super_get','rt_min','cn',15,true,60),
    ('tushare_super_get','stock_basic','cn',15,true,60),
    ('tushare_backup','reference_data','cn',40,true,6),
    ('tushare_backup','stock_basic','cn',40,true,6),
    ('baostock','daily_bar','cn',30,true,60),
    ('akshare','daily_bar','cn',40,true,30),
    ('akshare','market_summary','cn',60,true,10),
    ('akshare','market_breadth','cn',60,true,10),
    ('akshare','board_taxonomy','cn',60,true,6),
    ('akshare','moneyflow_supplement','cn',60,true,10),
    ('akshare','lhb_event','cn',60,true,10),
    ('akshare','lhb_supplement','cn',60,true,6),
    ('akshare','strong_pool','cn',60,true,10),
    ('akshare','limit_pool','cn',60,true,10),
    ('akshare','block_trade_supplement','cn',60,true,6),
    ('akshare','corporate_risk_supplement','cn',60,true,6),
    ('akshare','analyst_heat_supplement','cn',60,true,6),
    ('akshare','index_fund_supplement','cn',60,true,6),
    ('akshare','macro_cross_asset_supplement','cn',70,true,3),
    ('eastmoney_free','daily_bar','cn',45,true,30),
    ('eastmoney_free','realtime_quote','cn',45,true,10),
    ('tencent_free','daily_bar','cn',50,true,20),
    ('sina_free','realtime_quote','cn',55,true,10),
    ('cninfo_free','announcement','cn',35,true,20),
    ('remote_archive','analyst_report','cn',10,true,60)
ON CONFLICT(provider_key,capability,market) DO UPDATE SET
    priority=EXCLUDED.priority,enabled=EXCLUDED.enabled,
    rate_limit_per_minute=EXCLUDED.rate_limit_per_minute;

INSERT INTO quant.universe_members(universe_key,symbol,source,priority)
SELECT 'core', symbol, 'bootstrap-existing-instruments', 100
FROM quant.instruments WHERE symbol <> '000300.SH'
ON CONFLICT(universe_key,symbol) DO NOTHING;

INSERT INTO quant.factor_registry(factor_key,label,category,implementation,inputs,formula,framework_tags,version,status,metadata) VALUES
    ('momentum_5d','5日动量','momentum','native', '["close"]', '{"window":5,"operator":"return"}', '["qlib-compatible","alpha101-inspired"]', 'factor-v1','experimental','{}'),
    ('momentum_20d','20日动量','momentum','native', '["close"]', '{"window":20,"operator":"return"}', '["qlib-compatible","alpha101-inspired"]', 'factor-v1','experimental','{}'),
    ('reversal_5d','5日反转','reversal','native', '["close"]', '{"window":5,"operator":"negative_return"}', '["alpha101-inspired"]', 'factor-v1','experimental','{}'),
    ('sma_gap_20d','20日均线偏离','trend','native', '["close"]', '{"window":20,"operator":"close_over_sma_minus_one"}', '["qlib-compatible"]', 'factor-v1','experimental','{}'),
    ('volatility_20d','20日波动率','risk','native', '["close"]', '{"window":20,"operator":"std_return"}', '["qlib-compatible"]', 'factor-v1','experimental','{}'),
    ('volume_ratio_20d','20日量比','liquidity','native', '["volume"]', '{"window":20,"operator":"volume_over_mean"}', '["qlib-compatible"]', 'factor-v1','experimental','{}'),
    ('intraday_time_bucket_volume_surprise_v1','同刻分钟量能惊喜','liquidity','event-replay', '["intraday_minute_sessions.volume","intraday_minute_sessions.minute_bucket"]', '{"operator":"current_minute_volume_over_prior_session_same_bucket_median","minimum_sample_days":8}', '["point-in-time","research-only","tushare-super-get"]', 'time-bucket-volume-v1','experimental','{"retention_days":90,"no_auto_order":true,"scope":"explicit_watchlist_only"}'),
    ('base_contraction_30d','30日横盘波动收敛','price_action','native', '["high","low","close","volume"]', '{"window":30,"operators":["range_compression","realized_volatility_contraction","volume_dry_up","support_tests","near_resistance"]}', '["qlib-compatible","point-in-time","research-only"]', 'base-contraction-v1','experimental','{"no_auto_order":true}'),
    ('intraday_strength','日内收盘强度','price_action','native', '["high","low","close"]', '{"operator":"close_position"}', '["alpha101-inspired"]', 'factor-v1','experimental','{}'),
    ('moneyflow_dc_rate','东财主力净流入占比','flow','native', '["moneyflow_dc.net_amount_rate"]', '{"operator":"raw_point_in_time"}', '["tushare"]', 'factor-v1','experimental','{}'),
    ('analyst_market_consensus_3d','分析师市场文字共识','sentiment','native', '["remote_reports.summary","remote_reports.sections"]', '{"operator":"analyst_deduplicated_time_decay","half_life_days":3}', '["text-only","point-in-time"]', 'analyst-text-consensus-v1','experimental','{"no_media":true,"no_auto_order":true}'),
    ('analyst_theme_consensus_3d','分析师主题文字共识','sentiment','native', '["analyst_claims.theme"]', '{"operator":"analyst_report_theme_deduplicated_time_decay","half_life_days":3}', '["text-only","point-in-time"]', 'analyst-text-consensus-v1','experimental','{"no_media":true,"no_auto_order":true}'),
    ('analyst_text_freshness_3d','分析师文本新鲜度','data_quality','native', '["remote_reports.remote_updated_at"]', '{"operator":"mean_exponential_decay","half_life_days":3}', '["text-only"]', 'analyst-text-consensus-v1','experimental','{"no_media":true}')
ON CONFLICT(factor_key) DO NOTHING;

INSERT INTO quant.strategy_registry(strategy_key,label,engine,version,configuration,status) VALUES
    ('multi_factor_rank_v1','多因子横截面排序','native-a-share-simulator','strategy-v1',
     '{"factors":["momentum_20d","sma_gap_20d","volume_ratio_20d","reversal_5d"],"rebalance_days":5,"hold_days":5,"top_n":20,"long_only":true}', 'experimental'),
    ('intraday_green_reclaim_research_v1','盘中绿转红与板块共振研究','event_replay','research-v1',
     '{"status":"research_only","entry_window_pct":[0.5,6.5],"return_3m_pct":[1.5,4.5],"minute_volume_multiple_min":3.0,"above_vwap_pct":[0.0,6.0],"recovery_from_session_low_pct_min":3.0,"flow_percentile_min":0.9,"confirmation":"two_scans_or_peer_breadth","no_automatic_order":true}', 'experimental')
    ,('intraday_upside_breakout_research_v1','盘中首突破量价资金共振研究','event_replay','research-v1',
     '{"status":"research_only","entry_window_pct":[1.0,6.5],"return_1m_pct_min":0.25,"return_3m_pct":[1.2,3.5],"minute_volume_multiple_min":2.5,"above_vwap_pct":[0.2,5.5],"requires":"new_intraday_high_and_flow_or_peer_breadth","score_min":76,"confirmation":"two_scans_or_peer_breadth","no_automatic_order":true}', 'experimental')
    ,('intraday_eac_breakout_research_v2','盘中扩张承接延续研究','event_replay','research-v2',
     '{"status":"research_only","entry_window_pct":[1.0,6.5],"return_1m_pct_min":0.25,"return_3m_pct":[1.2,3.5],"minute_volume_multiple":[2.5,20.0],"above_vwap_pct":[0.2,5.5],"time_windows":["09:40-10:45","13:00-14:20"],"requires":"new_high_and_flow_or_two_peers_then_second_scan_acceptance","outlier_policy":"relative_volume_over_20_attention_only","no_automatic_order":true}', 'experimental')
    ,('intraday_eac_breakout_research_v3','盘中扩张承接延续与同刻量能研究','event_replay','research-v3',
     '{"status":"research_only","entry_window_pct":[1.0,6.5],"return_1m_pct_min":0.25,"return_3m_pct":[1.2,3.5],"minute_volume_multiple":[2.5,20.0],"time_bucket_volume_surprise_min":2.0,"time_bucket_minimum_sample_days":8,"above_vwap_pct":[0.2,5.5],"time_windows":["09:40-10:45","13:00-14:20"],"requires":"new_high_and_flow_or_two_peers_then_second_scan_acceptance_and_same_clock_volume_baseline","baseline_unavailable_policy":"attention_only","outlier_policy":"relative_volume_over_20_attention_only","no_automatic_order":true}', 'experimental')
    ,('post_close_base_contraction_v1','盘后横盘收敛蓄势研究','native-a-share-simulator','research-v1',
     '{"status":"research_only","lookback_days":30,"base_range_pct":[4,18],"recent_volatility_max_ratio":0.85,"recent_volume_max_ratio":0.80,"near_resistance_pct_max":3.0,"requires":"subsequent_eac_breakout_confirmation","no_automatic_order":true}', 'experimental')
ON CONFLICT(strategy_key) DO NOTHING;

INSERT INTO quant.research_frameworks(framework_key,label,role,integration_mode,status,license_note,prerequisites,metadata) VALUES
    ('native_factor_lab','本地因子实验室','factor calculation, IC and A-share simulation','in-process','native','project code', '[]', '{"feature_contract":"point-in-time canonical bars"}'),
    ('alphalens','AlphaLens-ReLoaded','factor diagnostics','metrics-compatible adapter','adapter_ready','Apache-2.0', '["factor values","forward returns","sufficient cross-section"]', '{"integration":"native outputs match IC, quantile return and turnover concepts"}'),
    ('qlib','Microsoft Qlib','ML research and workflow','offline export adapter','adapter_ready','MIT', '["3+ years point-in-time history","delisting-aware universe","research worker"]', '{"datasets":["Alpha158","Alpha360"],"execution":"offline only"}'),
    ('lean','QuantConnect LEAN','strict execution backtest','external validation adapter','adapter_ready','Apache-2.0', '["A-share transaction model","broker/data bridge" ]', '{"roles":["universe","alpha","portfolio","risk","execution"]}'),
    ('finrl','FinRL / FinRL-X','RL research','GPU training adapter','planned','MIT / Apache-2.0', '["3+ years history","A-share gym environment","GPU worker"]', '{"production_policy":"research only until out-of-sample gates pass"}'),
    ('vectorbt','VectorBT Community','parameter sweep research','isolated lab adapter','planned','Apache-2.0 with Commons Clause', '["isolated research environment","license review"]', '{"not_for":"core production dependency"}')
ON CONFLICT(framework_key) DO NOTHING;
"""


def pool_settings(environ: Mapping[str, str] | None = None) -> dict[str, int]:
    """Bounded, process-local settings for synchronous repository access."""
    env = os.environ if environ is None else environ

    def bounded(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            return min(maximum, max(minimum, int(env.get(name, default))))
        except (TypeError, ValueError):
            return default

    minimum = bounded("QUANT_DB_POOL_MIN_SIZE", 1, 1, 8)
    maximum = bounded("QUANT_DB_POOL_MAX_SIZE", 12, minimum, 32)
    return {"min_size": minimum, "max_size": maximum, "timeout_seconds": bounded("QUANT_DB_POOL_TIMEOUT_SECONDS", 10, 1, 60)}


class Database:
    def __init__(self) -> None:
        self._connect_kwargs = {
            "host": os.getenv("PGHOST", "postgres"),
            "port": int(os.getenv("PGPORT", "5432")),
            "dbname": os.getenv("PGDATABASE", "n8n"),
            "user": os.getenv("PGUSER", "n8n"),
            "password": os.getenv("PGPASSWORD", ""),
            "row_factory": dict_row,
            "connect_timeout": 8,
        }
        self._pool_settings = pool_settings()
        # Keep construction side-effect free so import-time unit tests do not
        # open PostgreSQL connections.  Lifespan/open() establishes the pool.
        self._pool = ConnectionPool(
            conninfo="", kwargs=self._connect_kwargs, open=False,
            min_size=self._pool_settings["min_size"], max_size=self._pool_settings["max_size"],
            timeout=self._pool_settings["timeout_seconds"], max_waiting=64,
        )
        self._opened = False

    def open(self) -> None:
        if not self._opened:
            self._pool.open(wait=True, timeout=self._pool_settings["timeout_seconds"])
            self._opened = True

    def close(self) -> None:
        if self._opened:
            self._pool.close()
            self._opened = False

    def pool_status(self) -> dict[str, int | bool]:
        stats = self._pool.get_stats()
        return {
            "open": self._opened,
            "min_size": self._pool_settings["min_size"],
            "max_size": self._pool_settings["max_size"],
            "pool_size": int(stats.get("pool_size", 0)),
            "available": int(stats.get("pool_available", 0)),
            "waiting": int(stats.get("requests_waiting", 0)),
        }

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        self.open()
        with self._pool.connection() as connection:
            with connection.transaction():
                yield connection

    def migrate(self) -> None:
        """Legacy bootstrap only; new schema changes belong to Alembic."""
        with self.transaction() as connection:
            connection.execute(SCHEMA_SQL + PLATFORM_SCHEMA_SQL)

    def verify_versioned_schema(self) -> None:
        """Fail startup clearly instead of silently mutating a known schema."""
        required = (
            "quant.alembic_version", "quant.instruments", "quant.canonical_bars_daily",
            "quant.provider_health", "quant.fetch_runs", "quant.provider_rate_limit_slots",
        )
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT count(*)::int AS present FROM unnest(%s::text[]) AS name "
                "WHERE to_regclass(name) IS NOT NULL",
                (list(required),),
            ).fetchone()
        if int(row["present"] or 0) != len(required):
            raise RuntimeError(
                "versioned quant schema is incomplete; restore a backup or run one explicit "
                "legacy bootstrap before stamping the Alembic baseline"
            )

    def ping(self) -> None:
        with self.transaction() as connection:
            connection.execute("SELECT 1")
