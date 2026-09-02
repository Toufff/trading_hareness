"""Full-market event/cross-sectional research over the now ~512-day canonical history.

Every finding in docs/*RESEARCH*.md before this was either a single-digit
sample count (the countertrend-rebound "10/10 hit" was two calendar days) or
a hand-run one-off query never persisted anywhere queryable. These five
studies run against the whole ingested canonical_bars_daily history (not a
36-symbol watchlist), persist into quant.strategy_experiments alongside the
existing shadow-strategy research, and are re-runnable rather than static
prose. None of them changes a live threshold, a strategy's live_effect or an
analyst weight; they are descriptive_only evidence for the promotion
registries this codebase already requires before anything can go live.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from .market_rules import LIMIT_TOLERANCE, is_at_limit
from .sector_membership_repository import point_in_time_membership_predicate

from psycopg.types.json import Json

BENCHMARK_SYMBOL = "000300.SH"


def _persist_experiment(connection: Any, strategy_key: str, start_date: date, end_date: date,
                        status: str, parameters: dict[str, Any], metrics: dict[str, Any]) -> None:
    connection.execute(
        """INSERT INTO quant.strategy_experiments(strategy_key,universe_key,start_date,end_date,status,parameters,metrics)
           VALUES(%s,'all_a',%s,%s,%s,%s,%s)""",
        (strategy_key, start_date, end_date, status, Json(parameters), Json(metrics)),
    )


def _round(value: Any, digits: int = 4) -> float | None:
    return round(float(value), digits) if value is not None else None


# ---------------------------------------------------------------------------
# 1. Limit-up continuation
# ---------------------------------------------------------------------------

def research_limit_up_continuation(connection: Any, start_date: date, end_date: date) -> dict[str, Any]:
    """Next-session behavior after a limit-up close, split by first-board vs repeat and by whether the next open is fillable."""
    rows = connection.execute(
        f"""WITH all_bars AS (
                -- lag() must see every prior trading day (including ones with no
                -- limit_up value) or "previous day was also limit-up" silently
                -- compares against the previous *limit-up-eligible* day instead
                -- of the true previous session whenever a row has a null limit_up.
                SELECT symbol,trading_date,close,limit_up,is_suspended,
                  lag(close) OVER (PARTITION BY symbol ORDER BY trading_date) prev_close,
                  lag(limit_up) OVER (PARTITION BY symbol ORDER BY trading_date) prev_limit_up
                FROM quant.canonical_bars_daily
                WHERE trading_date BETWEEN %s::date - 10 AND %s
             ), hits AS (
                SELECT symbol,trading_date,close,limit_up,
                  prev_close IS NOT NULL AND prev_limit_up IS NOT NULL AND prev_close>=prev_limit_up-{LIMIT_TOLERANCE} AS prev_was_limit_up
                FROM all_bars
                WHERE trading_date BETWEEN %s AND %s AND limit_up IS NOT NULL AND NOT is_suspended
                  AND close>=limit_up-{LIMIT_TOLERANCE}
             ), next_session AS (
                SELECT h.symbol,h.trading_date,h.prev_was_limit_up,n.open,n.close nx_close,n.pre_close,n.limit_up nx_limit_up,n.is_suspended
                  FROM hits h
                  JOIN LATERAL (
                    SELECT open,close,pre_close,limit_up,is_suspended FROM quant.canonical_bars_daily b
                     WHERE b.symbol=h.symbol AND b.trading_date>h.trading_date ORDER BY b.trading_date LIMIT 1
                  ) n ON true
                 WHERE n.open>0 AND n.pre_close>0
             )
             SELECT prev_was_limit_up, count(*) n,
               avg((open>=nx_limit_up-{LIMIT_TOLERANCE})::int) pct_open_locked,
               avg(open/pre_close-1) avg_open_gap,
               avg(nx_close/open-1) FILTER (WHERE open<nx_limit_up-{LIMIT_TOLERANCE}) avg_open_to_close,
               avg((nx_close>open)::int) FILTER (WHERE open<nx_limit_up-{LIMIT_TOLERANCE}) hit_open_to_close,
               avg((nx_close>=nx_limit_up-{LIMIT_TOLERANCE})::int) pct_re_limit
             FROM next_session GROUP BY prev_was_limit_up""",
        (start_date, end_date, start_date, end_date),
    ).fetchall()
    cohorts = {}
    for row in rows:
        key = "repeat_board" if row["prev_was_limit_up"] else "first_board"
        cohorts[key] = {"n": row["n"], "pct_next_open_locked": _round(row["pct_open_locked"]),
                        "avg_next_open_gap": _round(row["avg_open_gap"]), "avg_open_to_close_when_fillable": _round(row["avg_open_to_close"]),
                        "hit_rate_open_to_close_when_fillable": _round(row["hit_open_to_close"]), "pct_re_limit_up": _round(row["pct_re_limit"])}
    total = sum(item["n"] for item in cohorts.values())
    status = "completed" if total >= 50 else "insufficient_history"
    metrics = {"cohorts": cohorts, "total_events": total,
              "notice": "next-session open/close only; a locked next-session open is reported, not silently excluded from the count."}
    _persist_experiment(connection, "event_research_limit_up_continuation_v1", start_date, end_date, status,
                        {"cohort_by": "prev_session_was_also_limit_up"}, metrics)
    return metrics


# ---------------------------------------------------------------------------
# 2. Daily-frequency volume surge (the one intraday rule with positive live evidence)
# ---------------------------------------------------------------------------

def research_daily_volume_surge(connection: Any, start_date: date, end_date: date,
                                horizons: tuple[int, ...] = (1, 3, 5, 10)) -> dict[str, Any]:
    """volume_ratio>=2.5 and turnover_rate>=5.0 on day T (the intraday volume_anomaly thresholds, at daily frequency)."""
    horizon_metrics: dict[str, Any] = {}
    for horizon in horizons:
        rows = connection.execute(
            f"""WITH signal AS (
                SELECT f.symbol,f.trading_date
                  FROM quant.daily_fundamentals f
                 WHERE f.trading_date BETWEEN %s AND %s AND f.volume_ratio>=2.5 AND f.turnover_rate>=5.0
             ), priced AS (
                SELECT s.symbol,s.trading_date,
                  (SELECT b.trading_date FROM quant.canonical_bars_daily b WHERE b.symbol=s.symbol AND b.trading_date>s.trading_date
                     ORDER BY b.trading_date LIMIT 1) entry_date
                FROM signal s
             ), entered AS (
                SELECT p.*,e.open entry_price,e.is_suspended entry_is_suspended,e.limit_up entry_limit_up
                  FROM priced p JOIN quant.canonical_bars_daily e ON e.symbol=p.symbol AND e.trading_date=p.entry_date
             ), exited AS (
                SELECT en.*, x.close exit_close
                  FROM entered en
                  JOIN LATERAL (
                    SELECT close FROM quant.canonical_bars_daily b WHERE b.symbol=en.symbol AND b.trading_date>=en.entry_date
                     ORDER BY b.trading_date OFFSET %s LIMIT 1
                  ) x ON true
                 WHERE NOT en.entry_is_suspended AND (en.entry_limit_up IS NULL OR en.entry_price<en.entry_limit_up-{LIMIT_TOLERANCE})
             )
             SELECT count(*) n, avg(exit_close/entry_price-1) avg_return, avg((exit_close>entry_price)::int) hit_rate
               FROM exited""",
            (start_date, end_date, horizon - 1),
        ).fetchone()
        horizon_metrics[f"{horizon}d"] = {"n": rows["n"], "avg_return": _round(rows["avg_return"]), "hit_rate": _round(rows["hit_rate"])}
    total = horizon_metrics.get("1d", {}).get("n") or 0
    status = "completed" if total >= 200 else "insufficient_history"
    metrics = {"by_horizon": horizon_metrics, "signal_condition": "volume_ratio>=2.5 and turnover_rate>=5.0",
              "notice": "next-session-open entry, locked/suspended entries excluded rather than credited"}
    _persist_experiment(connection, "event_research_daily_volume_surge_v1", start_date, end_date, status,
                        {"horizons": list(horizons)}, metrics)
    return metrics


# ---------------------------------------------------------------------------
# 3. Short-term reversal (the one factor with a significant negative sample-out IC)
# ---------------------------------------------------------------------------

def research_short_term_reversal(connection: Any, start_date: date, end_date: date,
                                 lookback_days: tuple[int, ...] = (5, 10, 20), horizon_days: int = 10) -> dict[str, Any]:
    """Decile forward return by trailing return; a negative decile-1-minus-decile-10 spread confirms reversal, not momentum."""
    lookback_metrics: dict[str, Any] = {}
    for lookback in lookback_days:
        rows = connection.execute(
            """WITH panel AS (
                SELECT symbol,trading_date,close,
                  lag(close,%s) OVER (PARTITION BY symbol ORDER BY trading_date) prior_close,
                  lead(close,%s) OVER (PARTITION BY symbol ORDER BY trading_date) forward_close,
                  is_suspended
                FROM quant.canonical_bars_daily
                WHERE trading_date BETWEEN %s AND %s
             ), scored AS (
                SELECT *, close/prior_close-1 trailing_return, forward_close/close-1 forward_return
                  FROM panel WHERE prior_close>0 AND forward_close IS NOT NULL AND NOT is_suspended
             ), ranked AS (
                SELECT *, ntile(10) OVER (PARTITION BY trading_date ORDER BY trailing_return) decile
                  FROM scored
             )
             SELECT decile, count(*) n, avg(forward_return) avg_forward_return
               FROM ranked GROUP BY decile ORDER BY decile""",
            (lookback, horizon_days, start_date, end_date),
        ).fetchall()
        deciles = {int(row["decile"]): {"n": row["n"], "avg_forward_return": _round(row["avg_forward_return"])} for row in rows}
        spread = None
        if deciles.get(1) and deciles.get(10) and deciles[1]["avg_forward_return"] is not None and deciles[10]["avg_forward_return"] is not None:
            spread = round(deciles[1]["avg_forward_return"] - deciles[10]["avg_forward_return"], 4)
        lookback_metrics[f"{lookback}d_lookback"] = {
            "deciles": deciles, "total_rows": sum(item["n"] for item in deciles.values()),
            "decile1_minus_decile10_forward_return": spread,
            "interpretation": "reversal (losers beat winners)" if spread is not None and spread > 0 else
                              "momentum (winners beat losers)" if spread is not None and spread < 0 else "inconclusive",
        }
    total_rows = sum(item["total_rows"] for item in lookback_metrics.values())
    status = "completed" if total_rows >= 10000 else "insufficient_history"
    metrics = {"by_lookback": lookback_metrics, "horizon_days": horizon_days,
              "notice": "close-to-close only (descriptive); not a fillable-entry backtest"}
    _persist_experiment(connection, "event_research_short_term_reversal_v1", start_date, end_date, status,
                        {"lookback_days": list(lookback_days), "horizon_days": horizon_days}, metrics)
    return metrics


# ---------------------------------------------------------------------------
# 4. Sector-flow reversal, re-run at stock level (board-level SFR already exists in
#    sector_flow_daily_outcomes; this asks whether member stocks show the same pattern)
# ---------------------------------------------------------------------------

def research_sector_flow_reversal_stock_level(connection: Any, start_date: date, end_date: date,
                                              horizon_days: int = 1) -> dict[str, Any]:
    """Member-stock forward return using only as-known-at membership evidence."""
    membership_predicate = point_in_time_membership_predicate("m", "s.trading_date")
    rows = connection.execute(
        f"""WITH signal AS (
                SELECT f.taxonomy_key,f.sector_key,f.trading_date,f.transition
                  FROM quant.sector_flow_daily_features f
                 WHERE f.trading_date BETWEEN %s AND %s AND f.transition IN ('reversal_in','reversal_out')
             ), members AS (
                SELECT s.transition,s.trading_date,m.symbol
                  FROM signal s JOIN quant.sector_membership_history m
                    ON m.taxonomy_key=s.taxonomy_key AND m.sector_key=s.sector_key
                    AND {membership_predicate}
             ), priced AS (
                SELECT me.transition,me.symbol,me.trading_date,
                  (SELECT b.trading_date FROM quant.canonical_bars_daily b WHERE b.symbol=me.symbol AND b.trading_date>me.trading_date
                     ORDER BY b.trading_date LIMIT 1) entry_date
                FROM members me
             ), entered AS (
                SELECT p.*,e.open entry_price,e.is_suspended entry_is_suspended,e.limit_up entry_limit_up,e.limit_down entry_limit_down
                  FROM priced p JOIN quant.canonical_bars_daily e ON e.symbol=p.symbol AND e.trading_date=p.entry_date
             ), exited AS (
                SELECT en.*, x.close exit_close
                  FROM entered en
                  JOIN LATERAL (
                    SELECT close FROM quant.canonical_bars_daily b WHERE b.symbol=en.symbol AND b.trading_date>=en.entry_date
                     ORDER BY b.trading_date OFFSET %s LIMIT 1
                  ) x ON true
                 WHERE NOT en.entry_is_suspended AND (en.entry_limit_up IS NULL OR en.entry_price<en.entry_limit_up-{LIMIT_TOLERANCE})
             )
             SELECT transition, count(*) n, count(DISTINCT symbol) symbols, count(DISTINCT trading_date) event_days,
               avg(exit_close/entry_price-1) avg_return, avg((exit_close>entry_price)::int) hit_rate
             FROM exited GROUP BY transition""",
        (start_date, end_date, horizon_days - 1),
    ).fetchall()
    cohorts = {row["transition"]: {"n": row["n"], "distinct_symbols": row["symbols"], "distinct_event_days": row["event_days"],
                                   "avg_return": _round(row["avg_return"]), "hit_rate": _round(row["hit_rate"])} for row in rows}
    total = sum(item["n"] for item in cohorts.values())
    min_days = min((item["distinct_event_days"] for item in cohorts.values()), default=0)
    status = "completed" if total >= 200 and min_days >= 20 else "insufficient_history"
    metrics = {"cohorts": cohorts, "total_events": total, "horizon_days": horizon_days,
              "membership_caveat": "strict point-in-time membership only; legacy_unbounded rows excluded"}
    _persist_experiment(connection, "event_research_sector_flow_reversal_stock_v1", start_date, end_date, status,
                        {"horizon_days": horizon_days}, metrics)
    return metrics


# ---------------------------------------------------------------------------
# 5. Post-close three-path candidate screening, sampled across the full history
# ---------------------------------------------------------------------------

def research_post_close_backtest(connection: Any, start_date: date, end_date: date, *,
                                 sample_every_n_days: int = 10, lookback_days: int = 30,
                                 horizon_days: int = 10) -> dict[str, Any]:
    """Re-run the real post_close_structures.py scoring across sampled historical dates.

    This reuses the exact production classification functions
    (daily_base_structure / post_close_forming_structure /
    post_close_fresh_start_structure) with their real length gates
    (30/15/15 sessions) - it does not reimplement or approximate the scoring
    formulas. It deliberately differs from live screen_candidates() in one
    way: each structure is evaluated as its own independent research cohort
    (a symbol can appear in more than one cohort the same day) instead of
    being collapsed into one best-scoring candidate per symbol - that
    collapsing exists in the live screen only to pick one display candidate,
    which would understate each structure's own forward-return sample here.
    board_context is unavailable for a historical scan at this scale, so
    board_bonus is 0 for every candidate - a real simplification versus the
    live board-aware score, stated in the persisted metrics. Sampling every
    ``sample_every_n_days`` trading days keeps this tractable; set it to 1
    for an exhaustive (slow) run.
    """
    from .post_close_structures import daily_base_structure, post_close_forming_structure, post_close_fresh_start_structure

    # This VM runs with a small fixed memory budget shared by Postgres and
    # every other service container. Fetching all ~5,600 symbols' bars for
    # one sampled day at once was observed to exhaust it. Processing in
    # bounded symbol batches keeps peak memory roughly constant regardless
    # of universe size, at the cost of more (cheap, indexed) round trips.
    SYMBOL_BATCH_SIZE = 400

    trading_dates = [row["trading_date"] for row in connection.execute(
        """SELECT DISTINCT trading_date FROM quant.canonical_bars_daily
             WHERE trading_date BETWEEN %s AND %s ORDER BY trading_date""",
        (start_date, end_date),
    ).fetchall()]
    sampled_dates = trading_dates[::max(1, sample_every_n_days)]
    all_symbols = sorted(row["symbol"] for row in connection.execute(
        "SELECT DISTINCT symbol FROM quant.canonical_bars_daily WHERE trading_date BETWEEN %s AND %s",
        (start_date, end_date),
    ).fetchall())
    symbol_batches = [all_symbols[index:index + SYMBOL_BATCH_SIZE] for index in range(0, len(all_symbols), SYMBOL_BATCH_SIZE)]
    outcomes_by_type: dict[str, list[float]] = {"base_ready_30d": [], "base_forming_15d": [], "fresh_start_15d": []}
    scanned_days = 0
    for as_of in sampled_dates:
        day_had_data = False
        for batch in symbol_batches:
            rows = connection.execute(
                """SELECT symbol,trading_date,open,high,low,close,volume,amount,adj_factor,is_suspended,limit_up,limit_down
                     FROM quant.canonical_bars_daily
                    WHERE symbol=ANY(%s) AND trading_date<=%s AND trading_date>%s::date - (%s+15)
                    ORDER BY symbol,trading_date""",
                (batch, as_of, as_of, lookback_days),
            ).fetchall()
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                grouped.setdefault(str(row["symbol"]), []).append(dict(row))
            if not grouped:
                continue
            day_had_data = True
            entry_rows = {str(row["symbol"]): dict(row) for row in connection.execute(
                """SELECT DISTINCT ON (symbol) symbol,open,is_suspended,limit_up FROM quant.canonical_bars_daily
                     WHERE symbol=ANY(%s) AND trading_date>%s ORDER BY symbol,trading_date""",
                (batch, as_of),
            ).fetchall()}
            exit_rows_by_symbol = {str(row["symbol"]): row for row in connection.execute(
                """SELECT symbol,close FROM (
                       SELECT symbol,close,row_number() OVER (PARTITION BY symbol ORDER BY trading_date) rn
                         FROM quant.canonical_bars_daily WHERE symbol=ANY(%s) AND trading_date>%s
                   ) ranked WHERE rn=%s""",
                (batch, as_of, horizon_days),
            ).fetchall()}
            for symbol, bars in grouped.items():
                if bars[-1]["trading_date"] != as_of:
                    continue
                candidate_types: list[str] = []
                if len(bars) >= 30 and daily_base_structure(bars[-30:]).get("status") == "ready":
                    candidate_types.append("base_ready_30d")
                elif len(bars) >= 15 and post_close_forming_structure(bars).get("status") == "forming":
                    candidate_types.append("base_forming_15d")
                if len(bars) >= 15 and post_close_fresh_start_structure(bars).get("status") == "started":
                    candidate_types.append("fresh_start_15d")
                if not candidate_types:
                    continue
                entry = entry_rows.get(symbol)
                exit_row = exit_rows_by_symbol.get(symbol)
                if entry is None or exit_row is None or entry["is_suspended"]:
                    continue
                if entry["limit_up"] and is_at_limit(float(entry["open"]), float(entry["limit_up"])):
                    continue
                candidate_return = float(exit_row["close"]) / float(entry["open"]) - 1
                for candidate_type in candidate_types:
                    outcomes_by_type[candidate_type].append(candidate_return)
        if day_had_data:
            scanned_days += 1
    cohorts = {}
    for candidate_type, returns in outcomes_by_type.items():
        if not returns:
            cohorts[candidate_type] = {"n": 0}
            continue
        cohorts[candidate_type] = {"n": len(returns), "avg_return": round(sum(returns) / len(returns), 4),
                                   "hit_rate": round(sum(value > 0 for value in returns) / len(returns), 4)}
    total = sum(item["n"] for item in cohorts.values())
    status = "completed" if total >= 100 and scanned_days >= 20 else "insufficient_history"
    metrics = {"cohorts": cohorts, "total_candidates": total, "scanned_trading_days": scanned_days,
              "sampled_trading_days": len(sampled_dates), "sample_every_n_days": sample_every_n_days,
              "board_bonus": "unavailable_for_historical_scan_set_to_zero"}
    _persist_experiment(connection, "event_research_post_close_backtest_v1", start_date, end_date, status,
                        {"sample_every_n_days": sample_every_n_days, "lookback_days": lookback_days, "horizon_days": horizon_days},
                        metrics)
    return metrics


ALL_STUDIES = (
    research_limit_up_continuation, research_daily_volume_surge, research_short_term_reversal,
    research_sector_flow_reversal_stock_level, research_post_close_backtest,
)


__all__ = [
    "ALL_STUDIES", "research_daily_volume_surge", "research_limit_up_continuation",
    "research_post_close_backtest", "research_sector_flow_reversal_stock_level", "research_short_term_reversal",
]
