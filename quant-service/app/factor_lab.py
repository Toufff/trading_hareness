"""Point-in-time factor research and a deliberately conservative A-share simulator.

This module has no dependency on a third-party backtest runtime.  It keeps the
online service small while producing portable factor/return artifacts that can
later be checked with AlphaLens, Qlib, LEAN, or a GPU research worker.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from .market_rules import a_share_limit_ratio
from .research_prices import adjusted_value
from .universe_history import point_in_time_membership_predicate
from .backtest_execution_rules import a_share_exit_lag


FACTOR_DIRECTIONS = {
    "momentum_5d": 1.0,
    "momentum_20d": 1.0,
    "reversal_5d": 1.0,
    "sma_gap_20d": 1.0,
    "volatility_20d": -1.0,
    "volume_ratio_20d": 1.0,
    "intraday_strength": 1.0,
}

# The public factor routes use ``factor_sql_lab`` where ranks and windows stay
# in PostgreSQL.  This older, pure-Python evaluator is retained for small
# parity checks only; materialising an all-A history as Python dictionaries can
# exhaust the API worker before it can return an honest ``insufficient``
# result.
MAX_LEGACY_UNIVERSE_SYMBOLS = 250


class LegacyFactorEngineLimitError(ValueError):
    """Raised before the compatibility evaluator can exhaust worker memory."""


def value(raw: Any) -> float | None:
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def adjusted_price(row: dict[str, Any], field: str = "close") -> float | None:
    """Return a research price adjusted by the same-day Tushare factor.

    Canonical OHLC remains raw for execution, limit checks and auditability.
    Factor/return calculations use this explicit view so an ex-right date does
    not become a false momentum, volatility, or moving-average event.
    """
    return adjusted_value(row, field)


def standard_deviation(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    center = sum(values) / len(values)
    return math.sqrt(sum((item - center) ** 2 for item in values) / (len(values) - 1))


def rank(values: list[float]) -> list[float]:
    """Average ranks for ties, scaled only by callers that need 0..1 values."""
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    output = [0.0] * len(values)
    cursor = 0
    while cursor < len(indexed):
        end = cursor
        while end + 1 < len(indexed) and indexed[end + 1][1] == indexed[cursor][1]:
            end += 1
        average_rank = (cursor + end) / 2 + 1
        for position in range(cursor, end + 1):
            output[indexed[position][0]] = average_rank
        cursor = end + 1
    return output


def pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_mean, right_mean = sum(left) / len(left), sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    left_scale = math.sqrt(sum((a - left_mean) ** 2 for a in left))
    right_scale = math.sqrt(sum((b - right_mean) ** 2 for b in right))
    return numerator / (left_scale * right_scale) if left_scale and right_scale else None


def max_drawdown(equity: list[float]) -> float:
    peak, result = 1.0, 0.0
    for point in equity:
        peak = max(peak, point)
        result = min(result, point / peak - 1)
    return result


def load_universe_bars(connection: Any, universe_key: str, start_date: date, end_date: date) -> dict[str, list[dict[str, Any]]]:
    """Load only the factor window, never every canonical bar before ``end_date``.

    The compatibility evaluator computes at most a 20-trading-day lookback,
    so a 120-calendar-day warmup safely covers holidays without materializing
    multi-year full-market history in the API process.  The SQL evaluator is
    the preferred production research path; this bound keeps the legacy
    helper safe for small diagnostics and parity tests.
    """
    warmup_start = start_date - timedelta(days=120)
    membership_count = connection.execute(
        """SELECT count(DISTINCT membership.symbol)::int AS count
             FROM quant.universe_membership_history membership
            WHERE membership.universe_key=%s
              AND membership.effective_from<=%s
              AND (membership.effective_to IS NULL OR membership.effective_to>=%s)""",
        (universe_key, end_date, warmup_start),
    ).fetchone()
    symbol_count = int((membership_count or {}).get("count") or 0)
    if symbol_count > MAX_LEGACY_UNIVERSE_SYMBOLS:
        raise LegacyFactorEngineLimitError(
            f"legacy factor engine is limited to {MAX_LEGACY_UNIVERSE_SYMBOLS} symbols; "
            "use the bounded SQL factor engine for a broad universe"
        )

    membership_predicate = point_in_time_membership_predicate("membership", "b")
    rows = connection.execute(
        """SELECT b.symbol,b.trading_date,b.open,b.high,b.low,b.close,b.pre_close,b.volume,b.adj_factor,b.is_suspended,b.limit_up,b.limit_down,
                  i.is_st
           FROM quant.canonical_bars_daily b
           JOIN quant.universe_membership_history membership
             ON membership.universe_key=%s AND membership.symbol=b.symbol
            AND """ + membership_predicate + """
           JOIN quant.instruments i ON i.symbol=b.symbol
           WHERE b.trading_date BETWEEN %s AND %s
             AND (i.list_date IS NULL OR i.list_date<=b.trading_date)
             AND (i.delist_date IS NULL OR i.delist_date>=b.trading_date)
           ORDER BY b.symbol,b.trading_date""",
        (universe_key, warmup_start, end_date),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["symbol"])].append(dict(row))
    return dict(grouped)


def factor_at(bars: list[dict[str, Any]], index: int, factor_key: str) -> float | None:
    close = adjusted_price(bars[index])
    if close is None:
        return None
    if factor_key in {"momentum_5d", "reversal_5d"}:
        if index < 5 or not adjusted_price(bars[index - 5]):
            return None
        result = close / adjusted_price(bars[index - 5]) - 1  # type: ignore[arg-type]
        return -result if factor_key == "reversal_5d" else result
    if factor_key == "momentum_20d":
        if index < 20 or not adjusted_price(bars[index - 20]):
            return None
        return close / adjusted_price(bars[index - 20]) - 1  # type: ignore[arg-type]
    if factor_key == "sma_gap_20d":
        closes = [adjusted_price(row) for row in bars[index - 19:index + 1]] if index >= 19 else []
        if len(closes) != 20 or any(item is None for item in closes):
            return None
        baseline = sum(item for item in closes if item is not None) / 20
        return close / baseline - 1 if baseline else None
    if factor_key == "volatility_20d":
        if index < 20:
            return None
        closes = [adjusted_price(row) for row in bars[index - 20:index + 1]]
        if any(item in (None, 0) for item in closes):
            return None
        returns = [closes[offset] / closes[offset - 1] - 1 for offset in range(1, len(closes))]  # type: ignore[operator]
        return standard_deviation(returns)
    if factor_key == "volume_ratio_20d":
        if index < 19:
            return None
        volumes = [value(row.get("volume")) for row in bars[index - 19:index + 1]]
        if any(item is None for item in volumes):
            return None
        baseline = sum(item for item in volumes if item is not None) / 20
        return volumes[-1] / baseline if baseline else None  # type: ignore[operator]
    if factor_key == "intraday_strength":
        high, low = adjusted_price(bars[index], "high"), adjusted_price(bars[index], "low")
        return (close - low) / (high - low) if high is not None and low is not None and high > low else None
    return None


def historical_factor_panel(connection: Any, universe_key: str, start_date: date, end_date: date,
                            factor_keys: list[str]) -> tuple[dict[date, dict[str, dict[str, float]]], dict[str, list[dict[str, Any]]]]:
    bars_by_symbol = load_universe_bars(connection, universe_key, start_date, end_date)
    panel: dict[date, dict[str, dict[str, float]]] = defaultdict(dict)
    for symbol, bars in bars_by_symbol.items():
        for index, bar in enumerate(bars):
            values = {key: factor_at(bars, index, key) for key in factor_keys}
            panel[bar["trading_date"]][symbol] = {key: item for key, item in values.items() if item is not None}
    return dict(panel), bars_by_symbol


def evaluate_factor(connection: Any, factor_key: str, universe_key: str, start_date: date, end_date: date, horizon_days: int) -> dict[str, Any]:
    panel, bars_by_symbol = historical_factor_panel(connection, universe_key, start_date, end_date, [factor_key])
    index_by_date = {symbol: {row["trading_date"]: index for index, row in enumerate(bars)} for symbol, bars in bars_by_symbol.items()}
    daily_ic: list[float] = []
    daily_ic_points: list[dict[str, Any]] = []
    spreads: list[float] = []
    selected_sets: list[set[str]] = []
    observations = 0
    for trading_date in sorted(day for day in panel if start_date <= day <= end_date):
        entries: list[tuple[str, float, float]] = []
        for symbol, values in panel[trading_date].items():
            factor_value = values.get(factor_key)
            index = index_by_date[symbol].get(trading_date)
            bars = bars_by_symbol[symbol]
            if factor_value is None or index is None or index + horizon_days >= len(bars):
                continue
            entry, exit_price = adjusted_price(bars[index]), adjusted_price(bars[index + horizon_days])
            if entry is None or exit_price is None or not entry:
                continue
            entries.append((symbol, factor_value, exit_price / entry - 1))
        if len(entries) < 2:
            continue
        factors, returns = [entry[1] for entry in entries], [entry[2] for entry in entries]
        rank_ic = pearson(rank(factors), rank(returns))
        if rank_ic is not None:
            daily_ic.append(rank_ic)
            daily_ic_points.append({"date": str(trading_date), "rank_ic": rank_ic})
        ordered = sorted(entries, key=lambda item: item[1])
        bucket = max(1, len(ordered) // min(5, len(ordered)))
        spreads.append(average([item[2] for item in ordered[-bucket:]]) - average([item[2] for item in ordered[:bucket]]))  # type: ignore[operator]
        selected_sets.append({item[0] for item in ordered[-bucket:]})
        observations += len(entries)
    turnover = []
    for previous, current in zip(selected_sets, selected_sets[1:], strict=False):
        turnover.append(1 - len(previous.intersection(current)) / max(1, len(previous)))
    mean_ic, ic_std = average(daily_ic), standard_deviation(daily_ic)
    status = "completed" if len(daily_ic) >= 20 and observations >= 50 else "insufficient_history"
    return {
        "factor_key": factor_key, "universe_key": universe_key, "start_date": str(start_date), "end_date": str(end_date),
        "horizon_days": horizon_days, "status": status, "observations": observations, "cross_section_days": len(daily_ic),
        "metrics": {"rank_ic_mean": mean_ic, "rank_ic_std": ic_std, "rank_icir": mean_ic / ic_std if mean_ic is not None and ic_std else None,
                    "rank_ic_positive_ratio": sum(item > 0 for item in daily_ic) / len(daily_ic) if daily_ic else None,
                    "top_minus_bottom_return": average(spreads), "top_bucket_turnover": average(turnover),
                    "sample_gate": {"minimum_cross_section_days": 20, "minimum_observations": 50}},
        "artifact": {"daily_rank_ic": daily_ic_points,
                     "note": "Factor values and forward returns are point-in-time close-to-close research artifacts; not execution fills."},
    }


def run_multi_factor_strategy(connection: Any, universe_key: str, start_date: date, end_date: date, parameters: dict[str, Any]) -> dict[str, Any]:
    factor_keys = [str(item) for item in parameters.get("factors", ["momentum_20d", "sma_gap_20d", "volume_ratio_20d", "reversal_5d"])]
    rebalance_days = max(1, int(parameters.get("rebalance_days", 5)))
    hold_days = max(1, int(parameters.get("hold_days", 5)))
    exit_lag = a_share_exit_lag(hold_days)
    top_n = max(1, int(parameters.get("top_n", 20)))
    total_cost_bps = max(0.0, float(parameters.get("total_cost_bps", 18.0)))
    panel, bars_by_symbol = historical_factor_panel(connection, universe_key, start_date, end_date, factor_keys)
    index_by_date = {symbol: {row["trading_date"]: index for index, row in enumerate(bars)} for symbol, bars in bars_by_symbol.items()}
    curve: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    equity = 1.0
    periods = 0
    dates = [item for item in sorted(panel) if start_date <= item <= end_date]
    for offset, signal_date in enumerate(dates):
        if offset % rebalance_days:
            continue
        scores: list[tuple[str, float]] = []
        candidates = [(symbol, values) for symbol, values in panel[signal_date].items() if all(key in values for key in factor_keys)]
        if len(candidates) < 2:
            continue
        for factor_key in factor_keys:
            ranked = rank([values[factor_key] * FACTOR_DIRECTIONS.get(factor_key, 1.0) for _, values in candidates])
            for (symbol, _), score in zip(candidates, ranked, strict=True):
                scores.append((f"{symbol}:{factor_key}", score / len(candidates)))
        combined: dict[str, list[float]] = defaultdict(list)
        for compound, score in scores:
            symbol, _ = compound.split(":", 1)
            combined[symbol].append(score)
        selected = sorted(((symbol, sum(items) / len(items)) for symbol, items in combined.items()), key=lambda item: (-item[1], item[0]))[:top_n]
        period_returns: list[float] = []
        for symbol, score in selected:
            index = index_by_date[symbol].get(signal_date)
            bars = bars_by_symbol[symbol]
            if index is None or index + exit_lag >= len(bars):
                continue
            entry_bar, exit_bar = bars[index + 1], bars[index + exit_lag]
            entry = value(entry_bar.get("open")) or value(entry_bar.get("close"))
            exit_price = value(exit_bar.get("close"))
            blocked = bool(entry_bar.get("is_suspended")) or bool(exit_bar.get("is_suspended"))
            limit_up, limit_down = value(entry_bar.get("limit_up")), value(exit_bar.get("limit_down"))
            # Exact provider prices win.  The fallback keeps non-main-board
            # securities from silently becoming unrestricted when stk_limit
            # has not yet been backfilled.
            entry_pre_close = value(entry_bar.get("pre_close"))
            exit_pre_close = value(exit_bar.get("pre_close"))
            ratio = a_share_limit_ratio(symbol, bool(entry_bar.get("is_st")))
            if limit_up is None and entry_pre_close is not None:
                limit_up = round(entry_pre_close * (1 + ratio), 2)
            if limit_down is None and exit_pre_close is not None:
                limit_down = round(exit_pre_close * (1 - ratio), 2)
            blocked = blocked or (limit_up is not None and entry is not None and entry >= limit_up)
            blocked = blocked or (limit_down is not None and exit_price is not None and exit_price <= limit_down)
            if blocked or entry is None or exit_price is None or not entry:
                continue
            adjusted_entry = adjusted_price(entry_bar, "open") or adjusted_price(entry_bar)
            adjusted_exit = adjusted_price(exit_bar)
            if adjusted_entry is None or adjusted_exit is None or not adjusted_entry:
                continue
            gross = adjusted_exit / adjusted_entry - 1
            net = (1 + gross) * (1 - total_cost_bps / 10_000) ** 2 - 1
            period_returns.append(net)
            trades.append({"symbol": symbol, "signal_date": str(signal_date), "entry_date": str(entry_bar["trading_date"]),
                           "exit_date": str(exit_bar["trading_date"]), "score": score, "gross_return": gross,
                           "net_return": net, "cost_bps": total_cost_bps})
        if not period_returns:
            continue
        period_return = sum(period_returns) / len(period_returns)
        equity *= 1 + period_return
        periods += 1
        curve.append({"date": str(signal_date), "return": period_return, "equity": equity, "positions": len(period_returns)})
    returns = [item["return"] for item in curve]
    annualized_volatility = standard_deviation(returns)
    annualized_volatility = annualized_volatility * math.sqrt(252 / hold_days) if annualized_volatility else None
    annualized_return = equity ** (252 / max(1, periods * hold_days)) - 1 if periods else None
    metrics = {"total_return": equity - 1 if periods else None, "annualized_return": annualized_return,
               "annualized_volatility": annualized_volatility,
               "sharpe_zero_rf": annualized_return / annualized_volatility if annualized_return is not None and annualized_volatility else None,
               "max_drawdown": max_drawdown([item["equity"] for item in curve]), "win_rate": sum(item > 0 for item in returns) / len(returns) if returns else None,
               "periods": periods, "trades": len(trades), "assumptions": {"long_only": True, "signal_available": "close", "entry": "next trading day open or close fallback",
               "total_cost_bps_round_trip": total_cost_bps * 2, "effective_exit_lag": exit_lag,
               "blocked": ["suspended", "limit_up_entry", "limit_down_exit"]}}
    status = "completed" if periods >= 20 and len(trades) >= 20 else "insufficient_history"
    return {"strategy_key": "multi_factor_rank_v1", "universe_key": universe_key, "start_date": str(start_date), "end_date": str(end_date),
            "status": status, "parameters": {**parameters, "factors": factor_keys, "rebalance_days": rebalance_days, "hold_days": hold_days,
            "top_n": top_n, "total_cost_bps": total_cost_bps}, "metrics": metrics, "equity_curve": curve, "trades": trades[:500]}
