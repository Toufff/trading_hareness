"""Bounded-memory, point-in-time daily factor evaluation in PostgreSQL.

Only small daily diagnostics are returned to Python.  Full-market bars,
cross-sectional ranks and neutralization stay in temporary database tables so
one year of all-A history cannot exhaust the API process heap.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import mean, stdev
from typing import Any, Iterable

from .replay_readiness import P2_MIN_DAILY_CALENDAR_SPAN_DAYS, P2_MIN_FULL_CROSS_SECTION_DAYS


SQL_FACTOR_COLUMNS = {
    "momentum_5d": "momentum_5d",
    "momentum_20d": "momentum_20d",
    "reversal_5d": "reversal_5d",
    "sma_gap_20d": "sma_gap_20d",
    "volatility_20d": "volatility_20d",
    "volume_ratio_20d": "volume_ratio_20d",
    "intraday_strength": "intraday_strength",
}
FACTOR_DIRECTIONS = {
    "momentum_5d": 1.0,
    "momentum_20d": 1.0,
    "reversal_5d": 1.0,
    "sma_gap_20d": 1.0,
    "volatility_20d": -1.0,
    "volume_ratio_20d": 1.0,
    "intraday_strength": 1.0,
}
# Keep the formal factor gate aligned with the replay control plane.  The
# separate P2 calendar-span gate prevents a short but unusually dense sample
# from being mistaken for three years of history.
MIN_FORMAL_HISTORY_DAYS = P2_MIN_FULL_CROSS_SECTION_DAYS
MIN_FORMAL_HISTORY_CALENDAR_SPAN_DAYS = P2_MIN_DAILY_CALENDAR_SPAN_DAYS


def evaluable_factor_keys() -> frozenset[str]:
    return frozenset(SQL_FACTOR_COLUMNS)


def _formal_history_metrics(connection: Any, start_date: date, end_date: date) -> dict[str, int]:
    """Read the same three-year evidence contract used by replay readiness."""
    row = connection.execute(
        """SELECT count(DISTINCT trading_date)::int AS days,min(trading_date) AS first_date,max(trading_date) AS last_date
             FROM quant.daily_market_aggregates
            WHERE trading_date BETWEEN %s AND %s AND quality_flags='[]'::jsonb""",
        (start_date, end_date),
    ).fetchone() or {}
    first_date, last_date = row.get("first_date"), row.get("last_date")
    span = max(0, (last_date - first_date).days) if first_date and last_date else 0
    return {"days": int(row.get("days") or 0), "calendar_span_days": span}


def _formal_history_blockers(history: dict[str, int]) -> list[str]:
    return [
        blocker for blocker, blocked in (
            ("less_than_three_years_of_full_cross_sections", history["days"] < MIN_FORMAL_HISTORY_DAYS),
            ("less_than_three_calendar_year_span", history["calendar_span_days"] < MIN_FORMAL_HISTORY_CALENDAR_SPAN_DAYS),
        ) if blocked
    ]


def _average(values: Iterable[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return mean(clean) if clean else None


def _sample_std(values: Iterable[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return stdev(clean) if len(clean) >= 2 else None


def _normal_two_sided_p(t_stat: float | None) -> float | None:
    if t_stat is None or not math.isfinite(t_stat):
        return None
    return math.erfc(abs(t_stat) / math.sqrt(2.0))


def _series_metrics(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    center, spread = _average(values), _sample_std(values)
    t_stat = center / (spread / math.sqrt(len(values))) if center is not None and spread and values else None
    return {
        "days": len(values), "mean": center, "std": spread,
        "positive_ratio": sum(value > 0 for value in values) / len(values) if values else None,
        "date_cluster_t_stat": t_stat, "normal_approx_p_value": _normal_two_sided_p(t_stat),
    }


def _split_rows(rows: list[dict[str, Any]], horizon_days: int) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: row["trading_date"])
    total = len(ordered)
    train_end, validation_end = int(total * 0.60), int(total * 0.80)
    embargo = max(1, int(horizon_days))
    splits = {
        "train": ordered[:max(0, train_end - embargo)],
        "validation": ordered[min(total, train_end + embargo):max(min(total, train_end + embargo), validation_end - embargo)],
        "test": ordered[min(total, validation_end + embargo):],
    }
    return splits, {
        "method": "chronological_60_20_20_with_boundary_embargo",
        "embargo_trading_days": embargo,
        "total_cross_section_days": total,
        "dropped_boundary_days": total - sum(len(items) for items in splits.values()),
        "ranges": {
            key: {"start": str(items[0]["trading_date"]), "end": str(items[-1]["trading_date"]), "days": len(items)}
            if items else {"start": None, "end": None, "days": 0}
            for key, items in splits.items()
        },
    }


def _bh_q_values(p_values: dict[str, float | None]) -> dict[str, float | None]:
    valid = sorted((value, key) for key, value in p_values.items() if value is not None)
    count = len(valid)
    output: dict[str, float | None] = {key: None for key in p_values}
    running = 1.0
    for reverse_index in range(count - 1, -1, -1):
        value, key = valid[reverse_index]
        rank = reverse_index + 1
        running = min(running, float(value) * count / rank)
        output[key] = min(1.0, running)
    return output


def _max_drawdown(equity: list[float]) -> float:
    peak, drawdown = 1.0, 0.0
    for point in equity:
        peak = max(peak, point)
        drawdown = min(drawdown, point / peak - 1)
    return drawdown


def prepare_factor_panel(connection: Any, universe_key: str, start_date: date, end_date: date,
                         horizon_days: int) -> dict[str, Any]:
    """Create one transaction-scoped panel shared by every requested factor."""
    connection.execute("DROP TABLE IF EXISTS factor_sql_panel")
    connection.execute(
        """CREATE TEMP TABLE factor_sql_panel ON COMMIT DROP AS
           WITH calendar AS (
               SELECT calendar_date AS trading_date,
                      row_number() OVER(ORDER BY calendar_date)::int AS trading_index
                 FROM quant.market_trade_calendar
                WHERE exchange='SSE' AND is_open
                  AND calendar_date BETWEEN %s AND %s
           ), source AS (
               SELECT bar.symbol,bar.trading_date,calendar.trading_index,
                      (bar.open*bar.adj_factor)::double precision AS adjusted_open,
                      (bar.high*bar.adj_factor)::double precision AS adjusted_high,
                      (bar.low*bar.adj_factor)::double precision AS adjusted_low,
                      (bar.close*bar.adj_factor)::double precision AS adjusted_close,
                      bar.open::double precision AS raw_open,
                      bar.close::double precision AS raw_close,
                      bar.limit_up::double precision AS limit_up,
                      bar.limit_down::double precision AS limit_down,
                      bar.volume::double precision AS volume,bar.is_suspended,
                      coalesce(nullif(instrument.industry,''),'UNKNOWN') AS industry,
                      CASE WHEN fundamental.total_mv>0 THEN ln(fundamental.total_mv::double precision) END AS log_market_cap
                 FROM quant.canonical_bars_daily bar
                 JOIN calendar ON calendar.trading_date=bar.trading_date
                 JOIN quant.universe_membership_history membership
                   ON membership.universe_key=%s AND membership.symbol=bar.symbol
                  AND membership.effective_from<=bar.trading_date
                  AND (membership.effective_to IS NULL OR membership.effective_to>=bar.trading_date)
                 JOIN quant.instruments instrument ON instrument.symbol=bar.symbol
                 LEFT JOIN quant.daily_fundamentals fundamental
                   ON fundamental.symbol=bar.symbol AND fundamental.trading_date=bar.trading_date
                WHERE bar.adj_factor>0 AND bar.close>0
                  AND (instrument.list_date IS NULL OR instrument.list_date<=bar.trading_date)
                  AND (instrument.delist_date IS NULL OR instrument.delist_date>=bar.trading_date)
           ), returns AS (
               SELECT source.*,
                      CASE WHEN trading_index-lag(trading_index) OVER(PARTITION BY symbol ORDER BY trading_date)=1
                           THEN adjusted_close/nullif(lag(adjusted_close) OVER(PARTITION BY symbol ORDER BY trading_date),0)-1 END AS daily_return,
                      lag(adjusted_close,5) OVER(PARTITION BY symbol ORDER BY trading_date) AS close_5d_ago,
                      lag(trading_index,5) OVER(PARTITION BY symbol ORDER BY trading_date) AS index_5d_ago,
                      lag(adjusted_close,20) OVER(PARTITION BY symbol ORDER BY trading_date) AS close_20d_ago,
                      lag(trading_index,20) OVER(PARTITION BY symbol ORDER BY trading_date) AS index_20d_ago
                 FROM source
           ), features AS (
               SELECT returns.*,
                      CASE WHEN trading_index-index_5d_ago=5
                           THEN adjusted_close/nullif(close_5d_ago,0)-1 END AS momentum_5d,
                      CASE WHEN trading_index-index_20d_ago=20
                           THEN adjusted_close/nullif(close_20d_ago,0)-1 END AS momentum_20d,
                      CASE WHEN trading_index-index_5d_ago=5
                           THEN -(adjusted_close/nullif(close_5d_ago,0)-1) END AS reversal_5d,
                      CASE WHEN trading_index-min(trading_index) OVER(PARTITION BY symbol ORDER BY trading_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)=19
                           THEN adjusted_close/nullif(avg(adjusted_close) OVER(PARTITION BY symbol ORDER BY trading_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),0)-1 END AS sma_gap_20d,
                      CASE WHEN count(daily_return) OVER(PARTITION BY symbol ORDER BY trading_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)=20
                             AND trading_index-min(trading_index) OVER(PARTITION BY symbol ORDER BY trading_date ROWS BETWEEN 20 PRECEDING AND CURRENT ROW)=20
                           THEN stddev_samp(daily_return) OVER(PARTITION BY symbol ORDER BY trading_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) END AS volatility_20d,
                      CASE WHEN count(volume) OVER(PARTITION BY symbol ORDER BY trading_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)=20
                             AND trading_index-min(trading_index) OVER(PARTITION BY symbol ORDER BY trading_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)=19
                           THEN volume/nullif(avg(volume) OVER(PARTITION BY symbol ORDER BY trading_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),0) END AS volume_ratio_20d,
                      CASE WHEN adjusted_high>adjusted_low
                           THEN (adjusted_close-adjusted_low)/(adjusted_high-adjusted_low) END AS intraday_strength
                 FROM returns
           ) SELECT * FROM features""",
        (start_date - timedelta(days=120), end_date + timedelta(days=max(120, horizon_days * 3)), universe_key),
    )
    connection.execute("CREATE INDEX factor_sql_panel_symbol_index_idx ON factor_sql_panel(symbol,trading_index)")
    connection.execute("CREATE INDEX factor_sql_panel_date_idx ON factor_sql_panel(trading_date)")
    connection.execute("ANALYZE factor_sql_panel")
    row = connection.execute(
        "SELECT count(*)::int rows,count(DISTINCT symbol)::int symbols,count(DISTINCT trading_date)::int days FROM factor_sql_panel"
    ).fetchone()
    return dict(row or {})


def _materialize_factor_scores(connection: Any, factor_key: str, start_date: date, end_date: date) -> None:
    column = SQL_FACTOR_COLUMNS[factor_key]
    connection.execute("DROP TABLE IF EXISTS factor_sql_factor_scores")
    connection.execute(
        f"""CREATE TEMP TABLE factor_sql_factor_scores ON COMMIT DROP AS
            WITH candidate AS (
                SELECT signal.symbol,signal.trading_date,signal.industry,signal.log_market_cap,
                       signal.{column}::double precision AS raw_factor
                  FROM factor_sql_panel signal
                 WHERE signal.trading_date BETWEEN %s AND %s
                   AND signal.{column} IS NOT NULL
                   AND NOT coalesce(signal.is_suspended,false)
            ), bounds AS (
                SELECT trading_date,
                       percentile_cont(0.01) WITHIN GROUP(ORDER BY raw_factor) AS lower_factor,
                       percentile_cont(0.99) WITHIN GROUP(ORDER BY raw_factor) AS upper_factor
                  FROM candidate GROUP BY trading_date
            ), clipped AS (
                SELECT candidate.*,
                       greatest(bounds.lower_factor,least(bounds.upper_factor,candidate.raw_factor)) AS clipped_factor
                  FROM candidate JOIN bounds USING(trading_date)
            ), group_stats AS (
                SELECT trading_date,industry,
                       avg(clipped_factor) AS factor_mean,
                       avg(log_market_cap) AS size_mean,
                       coalesce(regr_slope(clipped_factor,log_market_cap),0) AS factor_size_slope
                  FROM clipped GROUP BY trading_date,industry
            ), neutral AS (
                SELECT clipped.*,
                       clipped_factor-stats.factor_mean-stats.factor_size_slope*
                         (coalesce(clipped.log_market_cap,stats.size_mean)-stats.size_mean) AS neutral_factor
                  FROM clipped JOIN group_stats stats USING(trading_date,industry)
            )
            SELECT neutral.*,
                   (neutral_factor-avg(neutral_factor) OVER(PARTITION BY trading_date)) /
                     nullif(stddev_samp(neutral_factor) OVER(PARTITION BY trading_date),0) AS factor_zscore
              FROM neutral""",
        (start_date, end_date),
    )
    connection.execute("CREATE INDEX factor_sql_factor_scores_date_symbol_idx ON factor_sql_factor_scores(trading_date,symbol)")
    connection.execute("ANALYZE factor_sql_factor_scores")


def _materialize_evaluation_rows(connection: Any, factor_key: str, start_date: date, end_date: date,
                                 horizon_days: int) -> None:
    _materialize_factor_scores(connection, factor_key, start_date, end_date)
    connection.execute("DROP TABLE IF EXISTS factor_sql_evaluation")
    connection.execute(
        """CREATE TEMP TABLE factor_sql_evaluation ON COMMIT DROP AS
            WITH outcome AS (
                SELECT scores.*,future.adjusted_close/nullif(signal.adjusted_close,0)-1 AS forward_return
                  FROM factor_sql_factor_scores scores
                  JOIN factor_sql_panel signal ON signal.symbol=scores.symbol AND signal.trading_date=scores.trading_date
                  JOIN factor_sql_panel future ON future.symbol=signal.symbol
                   AND future.trading_index=signal.trading_index+%s
                 WHERE scores.factor_zscore IS NOT NULL
                   AND NOT coalesce(future.is_suspended,false)
            ), return_stats AS (
                SELECT trading_date,industry,avg(forward_return) AS return_mean,
                       avg(log_market_cap) AS size_mean,
                       coalesce(regr_slope(forward_return,log_market_cap),0) AS return_size_slope
                  FROM outcome GROUP BY trading_date,industry
            ), neutral_return AS (
                SELECT outcome.*,
                       forward_return-stats.return_mean-stats.return_size_slope*
                         (coalesce(outcome.log_market_cap,stats.size_mean)-stats.size_mean) AS neutral_return
                  FROM outcome JOIN return_stats stats USING(trading_date,industry)
            )
            SELECT neutral_return.*,
                   rank() OVER(PARTITION BY trading_date ORDER BY raw_factor)::double precision AS raw_factor_rank,
                   rank() OVER(PARTITION BY trading_date ORDER BY forward_return)::double precision AS raw_return_rank,
                   rank() OVER(PARTITION BY trading_date ORDER BY factor_zscore)::double precision AS neutral_factor_rank,
                   rank() OVER(PARTITION BY trading_date ORDER BY neutral_return)::double precision AS neutral_return_rank,
                   ntile(5) OVER(PARTITION BY trading_date ORDER BY factor_zscore) AS neutral_bucket
              FROM neutral_return""",
        (horizon_days,),
    )
    connection.execute("CREATE INDEX factor_sql_evaluation_date_symbol_idx ON factor_sql_evaluation(trading_date,symbol)")
    connection.execute("ANALYZE factor_sql_evaluation")


def _factor_daily_rows(connection: Any) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT trading_date,count(*)::int AS observations,
                  corr(raw_factor_rank,raw_return_rank) AS raw_rank_ic,
                  corr(neutral_factor_rank,neutral_return_rank) AS neutral_rank_ic,
                  avg(neutral_return) FILTER(WHERE neutral_bucket=5)-
                    avg(neutral_return) FILTER(WHERE neutral_bucket=1) AS neutral_top_minus_bottom
             FROM factor_sql_evaluation GROUP BY trading_date ORDER BY trading_date"""
    ).fetchall()
    return [dict(row) for row in rows]


def _top_bucket_turnover(connection: Any) -> float | None:
    row = connection.execute(
        """WITH dates AS (
               SELECT trading_date,lag(trading_date) OVER(ORDER BY trading_date) AS previous_date
                 FROM (SELECT DISTINCT trading_date FROM factor_sql_evaluation) days
           ), counts AS (
               SELECT dates.trading_date,
                      count(previous.symbol)::int AS previous_count,
                      count(current.symbol) FILTER(WHERE previous.symbol IS NOT NULL)::int AS retained
                 FROM dates
                 JOIN factor_sql_evaluation previous ON previous.trading_date=dates.previous_date
                                                    AND previous.neutral_bucket=5
                 LEFT JOIN factor_sql_evaluation current ON current.trading_date=dates.trading_date
                                                       AND current.neutral_bucket=5
                                                       AND current.symbol=previous.symbol
                WHERE dates.previous_date IS NOT NULL GROUP BY dates.trading_date
           ) SELECT avg(1-retained::double precision/nullif(previous_count,0)) AS turnover FROM counts"""
    ).fetchone()
    return float(row["turnover"]) if row and row.get("turnover") is not None else None


def evaluate_factor_from_panel(connection: Any, factor_key: str, universe_key: str, start_date: date,
                               end_date: date, horizon_days: int, panel: dict[str, Any]) -> dict[str, Any]:
    if factor_key not in SQL_FACTOR_COLUMNS:
        raise ValueError(f"factor is not supported by sql evaluator: {factor_key}")
    _materialize_evaluation_rows(connection, factor_key, start_date, end_date, horizon_days)
    daily = _factor_daily_rows(connection)
    splits, split_contract = _split_rows(daily, horizon_days)
    split_metrics = {
        key: {
            "raw_rank_ic": _series_metrics(items, "raw_rank_ic"),
            "neutral_rank_ic": _series_metrics(items, "neutral_rank_ic"),
            "neutral_top_minus_bottom": _series_metrics(items, "neutral_top_minus_bottom"),
        }
        for key, items in splits.items()
    }
    observations = sum(int(row.get("observations") or 0) for row in daily)
    neutral_metrics = _series_metrics(daily, "neutral_rank_ic")
    history = _formal_history_metrics(connection, start_date, end_date)
    inferred_members = connection.execute(
        """SELECT count(*)::int AS count FROM quant.universe_membership_history
            WHERE universe_key=%s AND metadata->>'delist_date_quality'='inferred'
              AND effective_from<=%s AND effective_to>=%s""",
        (universe_key, end_date, start_date),
    ).fetchone()["count"]
    point_in_time_industry_ready = False
    promotion_ready = not _formal_history_blockers(history) and point_in_time_industry_ready
    status = "completed" if len(daily) >= 20 and observations >= 50 else "insufficient_history"
    return {
        "factor_key": factor_key, "universe_key": universe_key,
        "start_date": str(start_date), "end_date": str(end_date), "horizon_days": horizon_days,
        "status": status, "observations": observations, "cross_section_days": len(daily),
        "metrics": {
            "methodology_version": "sql-cross-section-v2",
            "raw_rank_ic": _series_metrics(daily, "raw_rank_ic"),
            "neutral_rank_ic": neutral_metrics,
            "neutral_top_minus_bottom": _series_metrics(daily, "neutral_top_minus_bottom"),
            "top_bucket_turnover": _top_bucket_turnover(connection),
            "walk_forward": split_metrics,
            "sample_gate": {"minimum_cross_section_days": 20, "minimum_observations": 50},
            "promotion_gate": {
                "status": "eligible_for_review" if promotion_ready else "research_only",
                "full_cross_section_days": history["days"],
                "required_full_cross_section_days": MIN_FORMAL_HISTORY_DAYS,
                "calendar_span_days": history["calendar_span_days"],
                "required_calendar_span_days": MIN_FORMAL_HISTORY_CALENDAR_SPAN_DAYS,
                "point_in_time_industry_history_ready": point_in_time_industry_ready,
                "required_point_in_time_industry_history": True,
                "blockers": [
                    *_formal_history_blockers(history),
                    *( ["point_in_time_industry_history_missing"] if not point_in_time_industry_ready else [] ),
                ],
                "live_strategy_effect": "none",
            },
        },
        "artifact": {
            "panel": panel,
            "daily_rank_ic": [
                {"date": str(row["trading_date"]), "raw_rank_ic": row.get("raw_rank_ic"),
                 "neutral_rank_ic": row.get("neutral_rank_ic"),
                 "neutral_top_minus_bottom": row.get("neutral_top_minus_bottom"),
                 "observations": row.get("observations")}
                for row in daily
            ],
            "split_contract": split_contract,
            "point_in_time_universe": {
                "table": "universe_membership_history", "inferred_delisting_intervals": int(inferred_members or 0),
                "notice": "Inferred delisting boundaries use the final canonical bar and remain quality-flagged.",
            },
            "preprocessing": {
                "winsorization": "daily 1st/99th percentile",
                "neutralization": "within-current-industry linear residual against point-in-time log daily total market value",
                "standardization": "daily cross-sectional z-score",
                "forward_return": "same symbol on exact SSE trading-calendar horizon",
                "history_continuity": "all lookback and forward windows require consecutive SSE trading indexes",
                "industry_quality": "current classification; formal promotion requires a point-in-time industry history",
            },
            "note": "Research artifact only; no execution fill or live threshold update.",
        },
    }


def evaluate_factor_set(connection: Any, factor_keys: list[str], universe_key: str, start_date: date,
                        end_date: date, horizon_days: int) -> list[dict[str, Any]]:
    unknown = sorted(set(factor_keys) - evaluable_factor_keys())
    if unknown:
        raise ValueError(f"unsupported sql factors: {', '.join(unknown)}")
    panel = prepare_factor_panel(connection, universe_key, start_date, end_date, horizon_days)
    results = [
        evaluate_factor_from_panel(connection, factor_key, universe_key, start_date, end_date, horizon_days, panel)
        for factor_key in factor_keys
    ]
    p_values = {
        result["factor_key"]: result["metrics"]["walk_forward"]["test"]["neutral_rank_ic"]["normal_approx_p_value"]
        for result in results
    }
    q_values = _bh_q_values(p_values)
    for result in results:
        result["metrics"]["multiple_testing"] = {
            "method": "benjamini_hochberg_on_test_date_cluster_normal_approximation",
            "tested_factors": len(results),
            "test_p_value": p_values[result["factor_key"]],
            "test_q_value": q_values[result["factor_key"]],
            "deflated_sharpe_ratio": None,
            "notice": "DSR is withheld until at least three years and a registered trial count are available.",
        }
    return results


def run_multi_factor_strategy_sql(connection: Any, universe_key: str, start_date: date, end_date: date,
                                  parameters: dict[str, Any]) -> dict[str, Any]:
    """Run a bounded, non-overlapping A-share research simulation.

    The signal is formed after the signal-date close, enters at the next exact
    trading-day open and cannot exit before the following trading day.  Full
    cross-sectional score tables remain in PostgreSQL; Python receives only
    period summaries and at most 500 audit trades.
    """
    factor_keys = [str(item) for item in parameters.get("factors", [])]
    unknown = sorted(set(factor_keys) - evaluable_factor_keys())
    if not factor_keys or unknown:
        suffix = f": {', '.join(unknown)}" if unknown else ""
        raise ValueError(f"strategy requires implemented sql factors{suffix}")
    rebalance_days = max(1, int(parameters.get("rebalance_days", 5)))
    hold_days = max(1, int(parameters.get("hold_days", 5)))
    exit_lag = max(2, hold_days)
    if rebalance_days < exit_lag:
        raise ValueError(
            f"rebalance_days must be at least {exit_lag} to avoid overlapping research periods"
        )
    top_n = max(1, int(parameters.get("top_n", 20)))
    cost_bps = max(0.0, float(parameters.get("total_cost_bps", 18.0)))
    panel = prepare_factor_panel(connection, universe_key, start_date, end_date, exit_lag)
    connection.execute("DROP TABLE IF EXISTS factor_sql_strategy_scores")
    connection.execute(
        """CREATE TEMP TABLE factor_sql_strategy_scores(
               factor_key text NOT NULL,symbol text NOT NULL,trading_date date NOT NULL,
               directed_zscore double precision NOT NULL) ON COMMIT DROP"""
    )
    for factor_key in factor_keys:
        _materialize_factor_scores(connection, factor_key, start_date, end_date)
        connection.execute(
            """INSERT INTO factor_sql_strategy_scores(factor_key,symbol,trading_date,directed_zscore)
               SELECT %s,symbol,trading_date,factor_zscore*%s
                 FROM factor_sql_factor_scores WHERE factor_zscore IS NOT NULL""",
            (factor_key, FACTOR_DIRECTIONS[factor_key]),
        )
    connection.execute("CREATE INDEX factor_sql_strategy_scores_date_symbol_idx ON factor_sql_strategy_scores(trading_date,symbol)")
    connection.execute("ANALYZE factor_sql_strategy_scores")
    connection.execute("DROP TABLE IF EXISTS factor_sql_strategy_trades")
    connection.execute(
        """CREATE TEMP TABLE factor_sql_strategy_trades ON COMMIT DROP AS
           WITH combined AS (
               SELECT symbol,trading_date,avg(directed_zscore) AS score
                 FROM factor_sql_strategy_scores
                GROUP BY symbol,trading_date
               HAVING count(DISTINCT factor_key)=%s
           ), signal_days AS (
               SELECT trading_date,row_number() OVER(ORDER BY trading_date)::int AS sequence
                 FROM (SELECT DISTINCT trading_date FROM combined) dates
           ), ranked AS (
               SELECT combined.*,row_number() OVER(
                          PARTITION BY combined.trading_date ORDER BY score DESC,symbol)::int AS candidate_rank
                 FROM combined JOIN signal_days USING(trading_date)
                WHERE mod(signal_days.sequence-1,%s)=0
           ), fills AS (
               SELECT ranked.*,entry.trading_date AS entry_date,exit_bar.trading_date AS exit_date,
                      entry.adjusted_open AS entry_price,exit_bar.adjusted_close AS exit_price,
                      exit_bar.adjusted_close/nullif(entry.adjusted_open,0)-1 AS gross_return
                 FROM ranked
                 JOIN factor_sql_panel signal ON signal.symbol=ranked.symbol
                                             AND signal.trading_date=ranked.trading_date
                 JOIN factor_sql_panel entry ON entry.symbol=signal.symbol
                                            AND entry.trading_index=signal.trading_index+1
                 JOIN factor_sql_panel exit_bar ON exit_bar.symbol=signal.symbol
                                               AND exit_bar.trading_index=signal.trading_index+%s
                WHERE ranked.candidate_rank<=%s
                  AND entry.raw_open>0 AND exit_bar.raw_close>0
                  AND entry.limit_up IS NOT NULL AND exit_bar.limit_down IS NOT NULL
                  AND NOT coalesce(entry.is_suspended,false)
                  AND NOT coalesce(exit_bar.is_suspended,false)
                  AND entry.raw_open<entry.limit_up
                  AND exit_bar.raw_close>exit_bar.limit_down
           )
           SELECT fills.*,(1+gross_return)*power(1-%s::double precision/10000,2)-1 AS net_return
             FROM fills""",
        (len(factor_keys), rebalance_days, exit_lag, top_n, cost_bps),
    )
    connection.execute("CREATE INDEX factor_sql_strategy_trades_date_idx ON factor_sql_strategy_trades(trading_date)")
    period_rows = [dict(row) for row in connection.execute(
        """SELECT trading_date,avg(net_return) AS period_return,count(*)::int AS positions
             FROM factor_sql_strategy_trades GROUP BY trading_date ORDER BY trading_date"""
    ).fetchall()]
    trade_count = int(connection.execute("SELECT count(*)::int AS count FROM factor_sql_strategy_trades").fetchone()["count"])
    trade_rows = [dict(row) for row in connection.execute(
        """SELECT symbol,trading_date AS signal_date,entry_date,exit_date,candidate_rank,score,
                  entry_price,exit_price,gross_return,net_return
             FROM factor_sql_strategy_trades ORDER BY trading_date,candidate_rank LIMIT 500"""
    ).fetchall()]
    equity, curve, returns = 1.0, [], []
    for row in period_rows:
        period_return = float(row["period_return"])
        returns.append(period_return)
        equity *= 1 + period_return
        curve.append({
            "date": str(row["trading_date"]), "return": period_return,
            "equity": equity, "positions": int(row["positions"]),
        })
    return_std = _sample_std(returns)
    annualized_volatility = return_std * math.sqrt(252 / rebalance_days) if return_std else None
    annualized_return = equity ** (252 / max(1, len(period_rows) * rebalance_days)) - 1 if period_rows else None
    history = _formal_history_metrics(connection, start_date, end_date)
    point_in_time_industry_ready = False
    promotion_ready = not _formal_history_blockers(history) and point_in_time_industry_ready
    metrics = {
        "total_return": equity - 1 if period_rows else None,
        "annualized_return": annualized_return,
        "annualized_volatility": annualized_volatility,
        "sharpe_zero_rf": annualized_return / annualized_volatility
        if annualized_return is not None and annualized_volatility else None,
        "max_drawdown": _max_drawdown([item["equity"] for item in curve]),
        "win_rate": sum(value > 0 for value in returns) / len(returns) if returns else None,
        "periods": len(period_rows), "trades": trade_count,
        "promotion_gate": {
            "status": "eligible_for_review" if promotion_ready else "research_only",
            "full_cross_section_days": history["days"],
            "required_full_cross_section_days": MIN_FORMAL_HISTORY_DAYS,
            "calendar_span_days": history["calendar_span_days"],
            "required_calendar_span_days": MIN_FORMAL_HISTORY_CALENDAR_SPAN_DAYS,
            "point_in_time_industry_history_ready": point_in_time_industry_ready,
            "required_point_in_time_industry_history": True,
            "blockers": [
                *_formal_history_blockers(history),
                *( ["point_in_time_industry_history_missing"] if not point_in_time_industry_ready else [] ),
            ],
            "live_strategy_effect": "none",
        },
        "assumptions": {
            "long_only": True, "signal_available": "after signal-date close",
            "entry": "next exact SSE trading-day raw open",
            "exit": f"signal trading index + {exit_lag}; never same-day as entry",
            "non_overlapping_periods": True, "single_side_cost_bps": cost_bps,
            "blocked": ["missing_exact_calendar_bar", "suspended", "limit_up_entry", "limit_down_exit", "missing_provider_limit"],
            "factor_preprocessing": "daily winsorized, current-industry and point-in-time size neutralized, z-scored",
            "industry_quality": "current classification; formal promotion requires point-in-time industry history",
        },
    }
    status = "completed" if len(period_rows) >= 20 and trade_count >= 20 else "insufficient_history"
    return {
        "strategy_key": "multi_factor_rank_v1", "universe_key": universe_key,
        "start_date": str(start_date), "end_date": str(end_date), "status": status,
        "parameters": {**parameters, "factors": factor_keys, "rebalance_days": rebalance_days,
                       "hold_days": hold_days, "effective_exit_lag": exit_lag,
                       "top_n": top_n, "total_cost_bps": cost_bps,
                       "engine": "native_factor_sql_v2", "panel": panel},
        "metrics": metrics, "equity_curve": curve,
        "trades": [{**row, "signal_date": str(row["signal_date"]),
                    "entry_date": str(row["entry_date"]), "exit_date": str(row["exit_date"])} for row in trade_rows],
    }


__all__ = [
    "MIN_FORMAL_HISTORY_DAYS", "SQL_FACTOR_COLUMNS", "evaluable_factor_keys",
    "evaluate_factor_from_panel", "evaluate_factor_set", "prepare_factor_panel",
    "run_multi_factor_strategy_sql",
]
