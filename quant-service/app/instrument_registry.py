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
     on the rows it genuinely *inserts*, but that does NOT mean it can only
     wait on a concurrent insert of a new key.  While it checks the unique
     index it has to know whether the conflicting row is live, so it waits
     for the transaction that last *wrote* that row to finish.  It therefore
     blocks on

     - the speculative-insertion token of a concurrent transaction inserting
       the same new key, and
     - an EXISTING row that another still-open transaction has UPDATEd --
       including through ``INSERT ... ON CONFLICT DO UPDATE``, i.e. every
       ``DO UPDATE`` writer below and ``upsert_daily_bar`` -- where the wait
       is on that transaction's xid ("waits for ShareLock on transaction
       ... while inserting index tuple ... in relation instruments", the
       wording of all four 2026-09-18 reports).

     It does not block on a plain ``SELECT``, ``SELECT ... FOR UPDATE`` /
     ``FOR KEY SHARE`` (a lock-only xmax) or another ``DO NOTHING`` that hit
     the same existing row: verified with two sessions on PostgreSQL 16.15
     against the production schema (the 2026-09-19 ``mech_check`` probe).
     So a ``DO NOTHING`` writer's deadlock exposure is any overlap with a
     concurrent writer that inserted OR updated one of its symbols, not only
     a batch of new listings.
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

Sorting is only half of the protection, because it binds only writers
that sort.  A writer outside this repository (the peer's per-row
registration in payload order) can still close a cycle with any sorted
writer, so every statement that writes this table is also executed through
``instrument_lock_retry.execute_instrument_write``: a savepoint with a
``lock_timeout`` below ``deadlock_timeout`` and a bounded, jittered retry,
which keeps the owner from ever being the deadlock victim and releases its
locks so the cycle breaks.  The same repository-wide guard test enforces
that no writer executes its statement any other way.

The helper is deliberately not a repository method and not part of
``main.py``: it is the shared write primitive for *bare symbol
registration*, called by ingestion repositories, services and runtime
actions.  ``ensure_named_instruments`` below is its sibling for the second
recurring shape -- symbol + exchange + a display ``name`` -- so the broker,
personal-decision and trade-discipline paths no longer each own a per-row
``DO UPDATE``.

Writers that carry more than that (industry, list/delist dates, ST flag)
still own their SQL, but they are no longer free-form: every
``quant.instruments`` writer under ``quant-service/app`` and ``scripts``
either lives in this module or sorts its rows in the statement itself with
``ORDER BY 1`` ahead of its ``ON CONFLICT`` clause and runs it through
``execute_instrument_write``, and
``tests/test_instrument_writer_lock_order.py`` walks the repository and
fails on the first exception.  That test, not a list in a document, is what
keeps the property true.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from .daily_bar_repository import exchange_for as default_exchange_for
from .instrument_lock_retry import execute_instrument_write

#: Symbols per statement.  A whole A-share cross-section (~5,500) still fits in
#: one round trip, while a historical backfill that hands over a much larger
#: symbol list cannot build an unbounded array parameter.
INSTRUMENT_CHUNK_SIZE = 5000

#: One statement, one conflict clause, two array parameters.  ``source`` is a
#: scalar because a single call always registers one payload's provenance.
ENSURE_INSTRUMENTS_SQL = (
    "INSERT INTO quant.instruments(symbol,exchange,source) "
    "SELECT t.symbol,t.exchange,%s FROM unnest(%s::text[],%s::text[]) AS t(symbol,exchange) "
    "ORDER BY 1 "
    "ON CONFLICT(symbol) DO NOTHING"
)

#: The second shape: symbol + exchange + a provider-supplied display ``name``,
#: with ``ON CONFLICT DO UPDATE`` touching **only** the name and only when the
#: incoming one is non-empty.  Five writers (broker order imports, broker trade
#: imports, both personal-decision sites and trade discipline) each owned a
#: byte-identical per-row copy of this statement; a per-row ``DO UPDATE`` is
#: the strongest lock class on this table, and the callers that loop over many
#: symbols in one transaction were taking those locks in operator/payload
#: order.  Note what the conflict clause deliberately does **not** touch:
#: ``exchange``, ``source`` and ``updated_at`` stay as they were, exactly as
#: the per-row statements left them, so this is a lock-order and round-trip
#: change and not a data change.
NAMED_INSTRUMENTS_SQL = (
    "INSERT INTO quant.instruments(symbol,exchange,name,source) "
    "SELECT t.symbol,t.exchange,t.name,%s "
    "FROM unnest(%s::text[],%s::text[],%s::text[]) AS t(symbol,exchange,name) "
    "ORDER BY 1 "
    "ON CONFLICT(symbol) DO UPDATE SET "
    "name=COALESCE(NULLIF(EXCLUDED.name,''),quant.instruments.name)"
)


def symbol_suffix_exchange(symbol: Any) -> str:
    """Return the bare Tushare suffix (``600519.SH`` -> ``SH``).

    Not the same value as ``daily_bar_repository.exchange_for`` (``SSE``):
    the broker, personal-decision and trade-discipline writers have stored
    the suffix form since before ``exchange_for`` existed, and their conflict
    clause never updates ``exchange``, so only a genuinely new row is
    affected by the difference.  Keeping the suffix resolver here means
    batching those writers changes no stored value.
    """
    return str(symbol).rsplit(".", 1)[-1]


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
        execute_instrument_write(
            connection, ENSURE_INSTRUMENTS_SQL,
            (source, [symbol for symbol, _exchange in chunk], [exchange for _symbol, exchange in chunk]),
            writer=f"instrument_registry.ensure_instruments:{source}",
        )
    return pairs


def named_instrument_rows(
    rows: Iterable[tuple[Any, Any]],
    *,
    exchange_for: Callable[[str], str] = symbol_suffix_exchange,
) -> list[tuple[str, str, str | None]]:
    """Return the ``(symbol, exchange, name)`` rows the named write will send.

    Pure, and it encodes the one semantic the per-row loops had for free.
    Those loops ran one statement per row, so a repeated symbol ended the
    transaction with the **last non-empty** name it was given (each statement
    applied ``COALESCE(NULLIF(new,''), current)`` to the result of the one
    before).  A single statement cannot reproduce that by replaying
    duplicates -- PostgreSQL rejects an ``ON CONFLICT DO UPDATE`` that would
    affect the same target row twice -- so the resolution moves here: walk
    the caller's order, keep the first sighting, and let any later non-blank
    name overwrite it.  A symbol whose every sighting is blank keeps a blank,
    which the conflict clause then turns back into the stored name.

    The result is sorted ascending: the same global lock order
    ``normalized_symbols`` fixes, which is the point of batching a
    ``DO UPDATE`` writer at all.
    """
    names: dict[str, str | None] = {}
    for symbol, name in rows:
        if symbol is None:
            continue
        key = str(symbol).strip()
        if not key:
            continue
        text = None if name is None else str(name)
        if key not in names or (text or "").strip():
            names[key] = text
    return [(symbol, exchange_for(symbol), names[symbol]) for symbol in sorted(names)]


def ensure_named_instruments(
    connection: Any,
    rows: Iterable[tuple[Any, Any]],
    source: str,
    *,
    exchange_for: Callable[[str], str] = symbol_suffix_exchange,
    chunk_size: int = INSTRUMENT_CHUNK_SIZE,
) -> list[tuple[str, str, str | None]]:
    """Register ``(symbol, name)`` pairs, ascending, one statement per chunk.

    Returns the rows actually written, in the order they were written.  Same
    contract as ``ensure_instruments``: a caller writing a child row that
    references ``quant.instruments(symbol)`` in the same transaction takes
    its symbols from this return value, not from its own raw input.
    """
    prepared = named_instrument_rows(rows, exchange_for=exchange_for)
    if not prepared:
        return []
    size = max(1, int(chunk_size))
    for start in range(0, len(prepared), size):
        chunk = prepared[start:start + size]
        execute_instrument_write(
            connection, NAMED_INSTRUMENTS_SQL,
            (source, [row[0] for row in chunk], [row[1] for row in chunk], [row[2] for row in chunk]),
            writer=f"instrument_registry.ensure_named_instruments:{source}",
        )
    return prepared


__all__ = [
    "ENSURE_INSTRUMENTS_SQL",
    "INSTRUMENT_CHUNK_SIZE",
    "NAMED_INSTRUMENTS_SQL",
    "ensure_instruments",
    "ensure_named_instruments",
    "instrument_pairs",
    "named_instrument_rows",
    "normalized_symbols",
    "symbol_suffix_exchange",
]
