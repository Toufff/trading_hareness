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
2. **A consistent lock order.**  Be precise about what each conflict action
   actually locks, because the two are not equivalent:

   * ``ON CONFLICT DO NOTHING`` (this statement) takes a row-level lock only
     on the rows it genuinely *inserts*.  An already-committed conflicting
     row is not locked at all; the one thing it can wait on is the
     *speculative insertion token* of a concurrent transaction inserting the
     same new key.  So its deadlock exposure is limited to two writers
     introducing overlapping **new** symbols in opposite orders -- which is
     exactly the 2026-09-18 pattern (a trading day's first sight of a batch
     of new listings), but nothing more.
   * ``ON CONFLICT DO UPDATE`` (``daily_bar_batch_repository``,
     ``daily_bar_repository`` and the other name/industry writers)
     additionally row-locks **every existing conflicting row**, i.e. the
     whole payload on any day after the first.  Those writers are therefore
     the ones that most need the shared order, and they must sort their
     arrays too -- registering symbols in ascending order here does not
     protect a DO UPDATE writer that re-locks the same table in payload
     order later in the same transaction.

   The PostgreSQL manual's deadlock section and the PostgreSQL wiki's
   deadlock/lock-monitoring advice both give the same remedy: make every
   application acquire locks on multiple objects in a consistent order.
   Sorting the symbols ascending before the write gives every writer that
   uses this helper one global order -- it is a correctness property, not a
   cosmetic detail, and must not be "optimized away" by preserving caller
   order.

The helper is deliberately not a repository method and not part of
``main.py``: it is the shared write primitive for *bare symbol
registration*, called by ingestion repositories, services and runtime
actions.  It is not the only writer of ``quant.instruments``: the paths that
also carry name/industry/list-date attributes still use their own
``DO UPDATE`` statements (see the list in ``AGENTS.md``).
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
    """Return exactly the registry keys that will be written, ascending.

    **Contract: this helper transforms, and what it returns is what is
    written.**  It strips surrounding whitespace, drops ``None`` and blanks,
    deduplicates and sorts, so the returned list -- not the caller's input --
    is the authoritative set of ``quant.instruments.symbol`` values.  A caller
    that also writes a child row carrying
    ``REFERENCES quant.instruments(symbol)`` in the same transaction **must
    write back these strings** (or the pairs ``ensure_instruments`` returns),
    never its own raw list.  Registering ``"000001.SZ"`` here while writing
    ``" 000001.SZ "`` into the child row is a mid-transaction foreign-key
    failure of an otherwise good batch: precisely the desynchronisation the
    case rule below refuses to introduce, so the transformation is made
    observable by returning its result rather than left implicit.

    Sorting is the shared lock order described in the module docstring; the
    deduplication also keeps one statement from touching the same conflict
    target twice.

    Case is deliberately **preserved**, not upper-cased.  Every caller writes
    the very same symbol string into a child table that carries
    ``REFERENCES quant.instruments(symbol)`` (minute sessions, market events,
    sector memberships, universe members) in the same transaction, so
    upper-casing only here would desynchronize the registry key from the row
    that references it and turn a caller-side normalization bug into a
    mid-transaction foreign-key failure of an otherwise good batch.
    Normalization therefore stays at each entry boundary, where it already
    is: ``request_models`` validates and upper-cases every API-supplied
    symbol against ``\\d{6}\\.(SH|SZ|BJ)``, and
    ``tushare_normalization`` / ``public_market_repository`` /
    ``sector_membership_repository`` / ``offline_minute_import_service``
    each ``.upper()`` and regex-check before calling this helper.
    """
    unique = {
        text
        for text in (str(symbol).strip() for symbol in symbols if symbol is not None)
        if text
    }
    return sorted(unique)


def instrument_pairs(
    symbols: Iterable[Any],
    *,
    exchange_for: Callable[[str], str] = default_exchange_for,
) -> list[tuple[str, str]]:
    """Return the ``(symbol, exchange)`` rows ``ensure_instruments`` will write.

    Pure: the same normalization and the same ascending order as the write,
    with the exchange resolved by the caller's own resolver.  It exists so a
    caller can compute the registry rows without a connection -- and so the
    write path and any caller-side assertion cannot drift apart.
    """
    return [(symbol, exchange_for(symbol)) for symbol in normalized_symbols(symbols)]


def ensure_instruments(
    connection: Any,
    symbols: Iterable[Any],
    source: str,
    *,
    exchange_for: Callable[[str], str] = default_exchange_for,
    chunk_size: int = INSTRUMENT_CHUNK_SIZE,
) -> list[tuple[str, str]]:
    """Register every symbol of one payload in ``quant.instruments``.

    Existing rows are left untouched (``ON CONFLICT DO NOTHING``), so this is
    safe to call for a mixed payload of known and unknown symbols.

    Returns **the ``(symbol, exchange)`` pairs actually written**, in the
    order they were written, not the caller's input.  Per
    ``normalized_symbols``' contract, a caller that writes a child row
    referencing ``quant.instruments(symbol)`` in the same transaction takes
    its symbols from this return value; anything the helper dropped (blank,
    ``None``) or rewrote (surrounding whitespace) has no instrument row and
    would fail that foreign key.
    """
    pairs = instrument_pairs(symbols, exchange_for=exchange_for)
    if not pairs:
        return []
    size = max(1, int(chunk_size))
    for start in range(0, len(pairs), size):
        chunk = pairs[start:start + size]
        connection.execute(
            ENSURE_INSTRUMENTS_SQL,
            (source, [symbol for symbol, _exchange in chunk], [exchange for _symbol, exchange in chunk]),
        )
    return pairs


__all__ = [
    "ENSURE_INSTRUMENTS_SQL",
    "INSTRUMENT_CHUNK_SIZE",
    "ensure_instruments",
    "instrument_pairs",
    "normalized_symbols",
]
