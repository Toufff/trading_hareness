"""Persist the daily multi-index regime label instead of computing it on demand.

``market_regimes.strategy_index_regime`` already classifies the market into
``corrective_rebound / trend_recovery / weak_or_declining / mixed_transition
/ insufficient_index_history`` from the four benchmark indices.  It was only
ever called live inside a review request and never stored, so no strategy or
backtest could stratify by regime.  This module reuses that exact
classification (it does not define a new one) and writes one row per trading
day so ``strategy_daily_candidates``/event-research queries can group by it.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from psycopg.types.json import Json

from .market_regimes import STRATEGY_INDEX_SYMBOLS, strategy_index_regime


def materialize_market_regime(connection: Any, trading_date: date) -> dict[str, Any]:
    """Classify and persist the regime for one already-closed trading day."""
    rows = connection.execute(
        """SELECT symbol,trading_date,close,high,low,volume FROM quant.canonical_bars_daily
             WHERE symbol=ANY(%s) AND trading_date<=%s
             ORDER BY symbol,trading_date""",
        (list(STRATEGY_INDEX_SYMBOLS), trading_date),
    ).fetchall()
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_symbol.setdefault(str(row["symbol"]), []).append(dict(row))
    # strategy_index_regime looks at each symbol's trailing window itself; cap
    # each symbol's input to its most recent 30 rows up to trading_date so a
    # long backfill does not scan the whole table per call.
    windowed = [item for values in by_symbol.values() for item in values[-30:]]
    result = strategy_index_regime(windowed)
    connection.execute(
        """INSERT INTO quant.market_regime_daily(trading_date,model_version,regime_label,index_count,
                  median_range_retracement,evidence)
           VALUES(%s,%s,%s,%s,%s,%s)
           ON CONFLICT(trading_date) DO UPDATE SET model_version=EXCLUDED.model_version,
             regime_label=EXCLUDED.regime_label,index_count=EXCLUDED.index_count,
             median_range_retracement=EXCLUDED.median_range_retracement,evidence=EXCLUDED.evidence,
             calculated_at=now()""",
        (trading_date, result["model_version"], result["state"], result["index_count"],
         result.get("median_range_retracement"), Json(result)),
    )
    return result


def backfill_market_regime(connection: Any, trading_dates: list[date]) -> int:
    """Materialize the regime for many already-known trading dates in one pass."""
    materialized = 0
    for trading_date in trading_dates:
        materialize_market_regime(connection, trading_date)
        materialized += 1
    return materialized


__all__ = ["materialize_market_regime", "backfill_market_regime"]
