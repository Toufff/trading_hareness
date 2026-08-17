"""Point-in-time research-universe maintenance.

The live ``universe_members`` table answers which symbols should be watched
now.  Historical factor research needs a different question: which symbols
were eligible on a past signal date?  This module maintains non-overlapping
membership intervals without rewriting the live control plane.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable


def sync_universe_membership_history(
    connection: Any,
    universe_key: str,
    exchange_date: date,
    active_symbols: Iterable[str],
    *,
    source: str,
    priority: int = 100,
) -> dict[str, int]:
    """Open/close PIT intervals for one authoritative live-universe snapshot."""
    symbols = sorted({str(symbol).strip().upper() for symbol in active_symbols if symbol})
    discarded_same_day = connection.execute(
        """DELETE FROM quant.universe_membership_history history
            WHERE history.universe_key=%s AND history.effective_to IS NULL
              AND history.effective_from=%s
              AND NOT (history.symbol=ANY(%s))""",
        (universe_key, exchange_date, symbols),
    ).rowcount
    closed = connection.execute(
        """UPDATE quant.universe_membership_history history
              SET effective_to=%s,updated_at=now(),
                  metadata=history.metadata || jsonb_build_object('closed_by',%s::text)
            WHERE history.universe_key=%s AND history.effective_to IS NULL
              AND history.effective_from<%s
              AND NOT (history.symbol=ANY(%s))""",
        (exchange_date - timedelta(days=1), source, universe_key, exchange_date, symbols),
    ).rowcount
    if symbols:
        opened = connection.execute(
            """INSERT INTO quant.universe_membership_history(
                   universe_key,symbol,effective_from,effective_to,source,priority,metadata)
               SELECT %s,candidate.symbol,%s,NULL,%s,%s,
                      jsonb_build_object('effective_from_basis','authoritative_live_snapshot')
                 FROM unnest(%s::text[]) AS candidate(symbol)
                WHERE NOT EXISTS (
                    SELECT 1 FROM quant.universe_membership_history current
                     WHERE current.universe_key=%s AND current.symbol=candidate.symbol
                       AND current.effective_to IS NULL)
               ON CONFLICT(universe_key,symbol,effective_from) DO UPDATE SET
                 effective_to=NULL,source=EXCLUDED.source,priority=EXCLUDED.priority,
                 metadata=quant.universe_membership_history.metadata || EXCLUDED.metadata,
                 updated_at=now()""",
            (universe_key, exchange_date, source, priority, symbols, universe_key),
        ).rowcount
    else:
        opened = 0
    return {
        "opened": int(opened or 0), "closed": int(closed or 0),
        "discarded_same_day": int(discarded_same_day or 0), "active": len(symbols),
    }


def point_in_time_membership_predicate(alias: str = "membership", bar_alias: str = "bar") -> str:
    """Return the shared ordered-date interval predicate for repository SQL.

    Repository queries use both ``bar`` and the shorter ``b`` alias.  Make the
    bar alias explicit rather than duplicating a subtly different temporal
    predicate in each evaluator.
    """
    return (
        f"{alias}.effective_from<={bar_alias}.trading_date AND "
        f"({alias}.effective_to IS NULL OR {alias}.effective_to>={bar_alias}.trading_date)"
    )


def rebuild_historical_membership_from_canonical(
    connection: Any, universe_key: str = "all_a",
) -> dict[str, int]:
    """Rebuild one research universe from local canonical daily evidence.

    This is a bounded local projection, not a live-universe refresh.  It keeps
    every A-share symbol that has a canonical bar in the requested retained
    history, uses the current ``all_a`` control plane only to identify symbols
    that remain open, and marks a non-current symbol's final-bar boundary as
    inferred when a supplier did not give a delisting date.  That is strictly
    better than silently filtering all delisted symbols out of factor studies,
    while still exposing the uncertainty to downstream reports.
    """
    automatic_sources = (
        "canonical_presence_plus_current_universe",
        "canonical_presence_delisting_proxy",
        "annual_daily_backfill_pit_active",
        "annual_daily_backfill_pit_inferred_delisting",
        "annual_daily_backfill_pit_supplier_delisting",
        "stock-basic-all-a:tushare_primary",
    )
    removed = connection.execute(
        """DELETE FROM quant.universe_membership_history
             WHERE universe_key=%s AND source=ANY(%s)""",
        (universe_key, list(automatic_sources)),
    ).rowcount
    inserted = connection.execute(
        """WITH canonical_presence AS (
               SELECT bar.symbol,min(bar.trading_date) AS first_bar_date,max(bar.trading_date) AS last_bar_date
                 FROM quant.canonical_bars_daily bar
                WHERE bar.symbol ~ '^((60[0135]|68[89])[0-9]{3}\\.SH|(000|001|002|003|300|301)[0-9]{3}\\.SZ|[489][0-9]{5}\\.BJ)$'
                GROUP BY bar.symbol
             ), desired AS (
               SELECT %s::text AS universe_key,bars.symbol,
                      greatest(bars.first_bar_date,coalesce(instrument.list_date,bars.first_bar_date)) AS effective_from,
                      CASE WHEN current.symbol IS NOT NULL THEN NULL
                           ELSE least(bars.last_bar_date,coalesce(instrument.delist_date,bars.last_bar_date)) END AS effective_to,
                      CASE WHEN current.symbol IS NOT NULL THEN 'annual_daily_backfill_pit_active'
                           WHEN instrument.delist_date IS NOT NULL THEN 'annual_daily_backfill_pit_supplier_delisting'
                           ELSE 'annual_daily_backfill_pit_inferred_delisting' END AS source,
                      jsonb_build_object(
                          'effective_from_basis',CASE WHEN instrument.list_date IS NULL THEN 'first_canonical_bar'
                              ELSE 'max_of_list_date_and_first_canonical_bar' END,
                          'effective_to_basis',CASE WHEN current.symbol IS NOT NULL THEN 'current_active_snapshot'
                              WHEN instrument.delist_date IS NOT NULL THEN 'supplier_delist_date_or_last_bar'
                              ELSE 'last_canonical_bar' END,
                          'delist_date_quality',CASE WHEN current.symbol IS NOT NULL THEN 'not_applicable'
                              WHEN instrument.delist_date IS NOT NULL THEN 'supplier' ELSE 'inferred' END
                      ) AS metadata
                 FROM canonical_presence bars
                 JOIN quant.instruments instrument ON instrument.symbol=bars.symbol
                 LEFT JOIN quant.universe_members current
                   ON current.universe_key=%s AND current.enabled AND current.symbol=bars.symbol
                WHERE instrument.delist_date IS NULL OR instrument.delist_date>=bars.first_bar_date
             )
           INSERT INTO quant.universe_membership_history(
               universe_key,symbol,effective_from,effective_to,source,priority,metadata
           )
           SELECT universe_key,symbol,effective_from,effective_to,source,100,metadata
             FROM desired
           ON CONFLICT(universe_key,symbol,effective_from) DO UPDATE SET
             effective_to=EXCLUDED.effective_to,source=EXCLUDED.source,priority=EXCLUDED.priority,
             metadata=EXCLUDED.metadata,updated_at=now()""",
        (universe_key, universe_key),
    ).rowcount
    return {"removed": int(removed or 0), "inserted": int(inserted or 0)}


__all__ = [
    "point_in_time_membership_predicate", "rebuild_historical_membership_from_canonical",
    "sync_universe_membership_history",
]
