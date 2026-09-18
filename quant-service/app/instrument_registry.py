"""Batched, deadlock-safe registration of symbols in ``quant.instruments``.

Almost every symbol-keyed table in this platform carries a
``REFERENCES quant.instruments(symbol)`` foreign key, so an ingestion path has
to guarantee the instrument row exists before it writes its own evidence.
That guarantee used to be bought one statement at a time
(``INSERT INTO quant.instruments ... VALUES(%s,%s,%s) ON CONFLICT DO NOTHING``
per row), which has two measured costs in the owner PostgreSQL log of
2026-09-18:

* Round trips.  A full A-share cross-section is ~5,547 symbols, i.e. ~5,547
  separate statements; over the peer's 52 ms tunnel that alone is minutes of
  pure latency.
* Deadlocks.  Three deadlocks (15:20, 15:22, 16:00) and 10-23 minute lock
  chains came from concurrent sessions inserting overlapping *new* symbol
  sets in different orders.

Both are addressed by the two properties this module owns:

1. **One statement per chunk.**  psycopg 3's "Fast execution helpers" guidance
   is explicit that a bulk write belongs in a single server round trip
   (``executemany``/``COPY``/one ``INSERT ... SELECT FROM unnest(...)``)
   rather than a Python-level loop of ``execute`` calls.  The ``unnest`` form
   is used here because it keeps ``ON CONFLICT (symbol) DO NOTHING`` on one
   statement.
2. **A consistent lock order.**  ``INSERT ... ON CONFLICT DO NOTHING`` takes a
   row-level lock on each inserted or conflicting key, so two transactions
   that insert the same two new symbols in opposite orders can wait on each
   other.  The PostgreSQL manual's deadlock section and the PostgreSQL wiki's
   deadlock/lock-monitoring advice both give the same remedy: make every
   application acquire locks on multiple objects in a consistent order.
   Sorting the symbols ascending before the write gives every writer in this
   platform one global order, which removes that deadlock cycle by
   construction -- it is a correctness property, not a cosmetic detail, and
   must not be "optimized away" by preserving caller order.

The helper is deliberately not a repository method and not part of
``main.py``: it is a single shared write primitive that ingestion
repositories, services and runtime actions all call.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from .daily_bar_repository import exchange_for as default_exchange_for

#: Symbols per statement.  A whole A-share cross-section (~5,500) still fits in
#: one round trip, while a historical backfill that hands over a much larger
#: symbol list cannot build an unbounded array parameter.
INSTRUMENT_CHUNK_SIZE = 5000

#: One statement, one conflict clause, two array parameters.  ``source`` is a
#: scalar because a single call always registers one payload's provenance.
ENSURE_INSTRUMENTS_SQL = (
    "INSERT INTO quant.instruments(symbol,exchange,source) "
    "SELECT t.symbol,t.exchange,%s FROM unnest(%s::text[],%s::text[]) AS t(symbol,exchange) "
    "ON CONFLICT(symbol) DO NOTHING"
)


def normalized_symbols(symbols: Iterable[Any]) -> list[str]:
    """Return the client-side deduplicated, blank-free, ascending symbol list.

    Sorting is the shared lock order described in the module docstring; the
    deduplication also keeps one statement from touching the same conflict
    target twice.
    """
    unique = {
        text
        for text in (str(symbol).strip() for symbol in symbols if symbol is not None)
        if text
    }
    return sorted(unique)


def ensure_instruments(
    connection: Any,
    symbols: Iterable[Any],
    source: str,
    *,
    exchange_for: Callable[[str], str] = default_exchange_for,
    chunk_size: int = INSTRUMENT_CHUNK_SIZE,
) -> list[str]:
    """Register every symbol of one payload in ``quant.instruments``.

    Existing rows are left untouched (``ON CONFLICT DO NOTHING``), so this is
    safe to call for a mixed payload of known and unknown symbols.  Returns
    the normalized symbol list actually sent, so a caller can assert on it.
    """
    ordered = normalized_symbols(symbols)
    if not ordered:
        return []
    size = max(1, int(chunk_size))
    for start in range(0, len(ordered), size):
        chunk = ordered[start:start + size]
        connection.execute(
            ENSURE_INSTRUMENTS_SQL,
            (source, chunk, [exchange_for(symbol) for symbol in chunk]),
        )
    return ordered


__all__ = [
    "ENSURE_INSTRUMENTS_SQL",
    "INSTRUMENT_CHUNK_SIZE",
    "ensure_instruments",
    "normalized_symbols",
]
