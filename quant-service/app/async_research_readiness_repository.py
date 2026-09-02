"""Native async read projections for research-readiness control-plane data."""

from __future__ import annotations

from typing import Any

from .async_market_result_read_repository import feature_readiness_estimated
from .replay_readiness import PIT_DAILY_COVERAGE_CTE, replay_readiness_payload
from .research_capacity import historical_capacity_plan


async def frameworks(async_database: Any) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        result = await connection.execute(
            "SELECT framework_key,label,role,integration_mode,status,license_note,prerequisites,metadata,updated_at FROM quant.research_frameworks ORDER BY framework_key"
        )
        rows = await result.fetchall()
    return {"items": rows}


async def feature_readiness(async_database: Any) -> dict[str, Any]:
    """Feature-readiness projection for the ``/api/v1/data-readiness/features`` route.

    Previously ran its own ``count(DISTINCT ...)``/``count(*)`` full scan of
    the largest control-plane tables (``canonical_bars_daily``,
    ``daily_fundamentals``, ...) on every dashboard refresh -- a byte-for-byte
    duplicate of the query in ``research_capacity.feature_readiness_state``
    (the synchronous path for the same route) with the async pool's 4
    connections shared across every other GET.  Both P0 blocker sources now
    read a ``pg_stat_all_tables`` estimate instead (see
    ``async_market_result_read_repository.feature_readiness_estimated``); the
    synchronous counterpart in ``research_capacity.py`` still needs the same
    change (that module belongs to a different work package -- see the WP10
    report's "needs other work package" section).
    """
    async with async_database.transaction() as connection:
        return await feature_readiness_estimated(connection)


async def replay_readiness(async_database: Any) -> dict[str, Any]:
    """Read bounded replay gates using the native async connection."""
    async with async_database.transaction() as connection:
        result = await connection.execute(
            f"""{PIT_DAILY_COVERAGE_CTE}
                SELECT
                  (SELECT min(trading_date) FROM daily_dates) first_daily_date,
                  (SELECT max(trading_date) FROM daily_dates) latest_daily_date,
                  (SELECT min(trading_date) FROM full_dates) first_full_cross_section_date,
                  (SELECT max(trading_date) FROM full_dates) latest_full_cross_section_date,
                  (SELECT count(*)::int FROM full_dates) full_cross_section_days,
                  (SELECT count(*)::int FROM daily_dates) daily_bar_days,
                  (SELECT count(*)::int FROM expected_universe) point_in_time_universe_days,
                  (SELECT count(*)::int FROM daily_dates dates
                    WHERE NOT EXISTS (SELECT 1 FROM expected_universe universe
                                      WHERE universe.trading_date=dates.trading_date)) missing_point_in_time_universe_days,
                  (SELECT count(DISTINCT (bar_time AT TIME ZONE 'Asia/Shanghai')::date)::int
                     FROM quant.market_bars_minute) offline_minute_trading_days,
                  (SELECT count(DISTINCT symbol)::int FROM quant.market_bars_minute) offline_minute_symbols,
                  (SELECT count(*)::int FROM quant.market_bars_minute) offline_minute_bars,
                  (SELECT count(*)::int FROM quant.market_bars_minute WHERE source_available_at IS NOT NULL) offline_minute_source_clock_bars,
                  (SELECT count(DISTINCT (source_available_at AT TIME ZONE 'Asia/Shanghai')::date)::int
                     FROM quant.market_bars_minute WHERE source_available_at IS NOT NULL) offline_minute_source_clock_days,
                  (SELECT count(DISTINCT (observed_at AT TIME ZONE 'Asia/Shanghai')::date)::int
                     FROM quant.intraday_rule_input_snapshots) forward_rule_input_days,
                  (SELECT count(*)::int FROM quant.intraday_rule_input_snapshots) forward_rule_input_rows,
                  (SELECT count(*)::int FROM quant.offline_imports WHERE status IN ('completed','partial')) completed_offline_imports,
                  (SELECT count(*)::int FROM quant.intraday_signal_events
                    WHERE state IN ('confirmed','alerted')) confirmed_signal_events,
                  (SELECT count(DISTINCT signal_event_id)::int FROM quant.intraday_signal_outcomes
                    WHERE status='matured') matured_signal_events"""
        )
        row = await result.fetchone()
    return replay_readiness_payload(dict(row or {}))


async def historical_estimate(async_database: Any, request: Any) -> dict[str, Any]:
    """Estimate local research capacity without a synchronous DB handoff."""
    async with async_database.transaction() as connection:
        if request.universe_symbols:
            universe_symbols = int(request.universe_symbols)
        else:
            result = await connection.execute(
                """SELECT coalesce(nullif((SELECT count(*)::int FROM quant.universe_members
                                             WHERE universe_key='all_a' AND enabled),0),
                              nullif((SELECT count(*)::int FROM quant.instruments
                                       WHERE symbol ~ '^\\d{6}\\.(SH|SZ|BJ)$'),0),5500) symbols"""
            )
            universe_symbols = int((await result.fetchone())["symbols"])
        result = await connection.execute(
            """SELECT api_name,ceil(avg(pg_column_size(row_data)))::int avg_bytes
                 FROM quant.tushare_raw_records
                WHERE api_name = ANY(%s) GROUP BY api_name""",
            (["daily", "adj_factor", "daily_basic", "stk_limit", "moneyflow_dc", "moneyflow",
              "cyq_perf", "cyq_chips", "stk_factor_pro", "suspend_d"],),
        )
        samples = await result.fetchall()
        result = await connection.execute("SELECT count(*)::int total FROM quant.sectors")
        sector_count = int((await result.fetchone())["total"])
        coverage_result = await connection.execute(
            """WITH daily_counts AS (
                 SELECT trading_date,count(DISTINCT symbol)::int symbols
                   FROM quant.canonical_bars_daily WHERE symbol<>'000300.SH' GROUP BY trading_date
               ), universe AS (
                 SELECT greatest(1,(SELECT count(*)::int FROM quant.universe_members
                                    WHERE universe_key='all_a' AND enabled)) AS symbols
               )
               SELECT (SELECT min(trading_date) FROM quant.canonical_bars_daily) first_bar_date,
                      (SELECT max(trading_date) FROM quant.canonical_bars_daily) latest_bar_date,
                      (SELECT count(*)::int FROM daily_counts) bar_days,
                      (SELECT count(*)::int FROM daily_counts,universe
                        WHERE daily_counts.symbols>=greatest(ceil(universe.symbols*0.8)::int,1000)) full_cross_section_days,
                      (SELECT max(symbols) FROM daily_counts) max_symbols_on_day,
                      (SELECT count(DISTINCT symbol)::int FROM quant.daily_fundamentals) fundamental_symbols,
                      (SELECT count(DISTINCT symbol)::int FROM quant.daily_trade_limits) limit_symbols,
                      (SELECT count(DISTINCT symbol)::int FROM quant.market_bars_minute) minute_symbols"""
        )
        coverage = dict(await coverage_result.fetchone() or {})
    return {
        **historical_capacity_plan(
            request.years, universe_symbols, request.trading_days_per_year, request.include_minute,
            {row["api_name"]: row["avg_bytes"] for row in samples}, sector_count,
        ),
        "current_coverage": coverage,
        "assumptions": {
            "storage_multiplier": 1.35,
            "row_size_source": "current raw samples when present, otherwise conservative constants",
            "minute_policy": "not included unless include_minute=true; historical minute remains offline-file only",
        },
    }
