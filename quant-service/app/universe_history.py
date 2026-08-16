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


__all__ = ["point_in_time_membership_predicate", "sync_universe_membership_history"]
