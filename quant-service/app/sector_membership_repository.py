"""Point-in-time sector-membership persistence and read predicates.

Provider constituent responses often describe the current snapshot but omit a
historical ``in_date``.  Treating that omission as 1900-01-01 makes a current
board composition appear to have been known throughout history.  This module
keeps that distinction explicit: supplier intervals and observed snapshots
have different bases, and every read is bounded by when the snapshot became
known.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo


PROVIDER_INTERVAL = "provider_interval"
OBSERVED_SNAPSHOT = "observed_snapshot"
LEGACY_UNBOUNDED = "legacy_unbounded"


def observed_exchange_date(observed_at: datetime) -> date:
    """Return the Shanghai exchange date on which a snapshot became known."""
    return observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()


def membership_interval(
    row: dict[str, Any],
    observed_at: datetime,
    *,
    parse_date: Callable[[Any], date | None],
) -> tuple[date, date | None, str, str]:
    """Derive a non-fictional interval and preserve its provenance basis."""
    effective_from = parse_date(row.get("in_date"))
    effective_to = parse_date(row.get("out_date"))
    if effective_from is not None:
        return effective_from, effective_to, PROVIDER_INTERVAL, PROVIDER_INTERVAL
    return observed_exchange_date(observed_at), effective_to, OBSERVED_SNAPSHOT, (
        PROVIDER_INTERVAL if effective_to is not None else OBSERVED_SNAPSHOT
    )


def point_in_time_membership_predicate(
    alias: str = "member",
    date_parameter: str = "%s",
    known_at_cutoff_sql: str | None = None,
) -> str:
    """Return a strict predicate for as-known-at sector membership joins.

    A later refresh may describe an older provider interval, but it must not
    be visible to a replay before that refresh was actually received.  Legacy
    rows with the old synthetic 1900 start date remain auditable but cannot
    silently enter research features.

    ``date_parameter`` appears up to three times in the returned fragment
    (twice directly, once inside the default ``known_at_cutoff_sql``), which
    used to force positional callers to repeat the same date literal three to
    six times.  Pass ``date_parameter="%(trade_date)s"`` to bind it once as
    ``{"trade_date": ...}`` in an otherwise fully named ``execute`` call
    instead; the default stays the positional ``%s`` so existing callers that
    build a plain tuple of parameters are unaffected.
    """
    cutoff = known_at_cutoff_sql or f"(({date_parameter}::date + time '17:00:00') AT TIME ZONE 'Asia/Shanghai')"
    return (
        f"{alias}.effective_from<={date_parameter} AND "
        f"({alias}.effective_to IS NULL OR {alias}.effective_to>={date_parameter}) AND "
        f"{alias}.effective_from_basis IN ('{PROVIDER_INTERVAL}','{OBSERVED_SNAPSHOT}') AND "
        f"{alias}.known_at <= {cutoff}"
    )


def persist_ths_snapshot(
    connection: Any,
    taxonomy_key: str,
    sector_key: str,
    rows: list[dict[str, Any]],
    provider_key: str,
    observed_at: datetime,
    *,
    ensure_instrument: Callable[[Any, str], None],
    parse_date: Callable[[Any], date | None],
) -> int:
    """Store one complete THS constituent response with explicit time basis."""
    active_members: set[str] = set()
    # Rows are collected here and written in one batched upsert below instead
    # of one INSERT per constituent (a THS concept/index snapshot can carry
    # hundreds of members).  ``ensure_instrument`` stays per-row: it is an
    # injected dependency owned outside this file.
    to_write: dict[tuple[str, date], tuple[Any, ...]] = {}
    for row in rows:
        symbol = str(row.get("con_code") or "").upper()
        if len(symbol) != 9 or symbol[6:] not in {".SH", ".SZ", ".BJ"} or not symbol[:6].isdigit():
            continue
        ensure_instrument(connection, symbol)
        effective_from, effective_to, from_basis, to_basis = membership_interval(
            row, observed_at, parse_date=parse_date,
        )
        to_write[(symbol, effective_from)] = (symbol, effective_from, effective_to, from_basis, to_basis, row)
        if effective_to is None:
            active_members.add(symbol)
    if to_write:
        entries = list(to_write.values())
        connection.execute(
            """INSERT INTO quant.sector_membership_history(
                   taxonomy_key,sector_key,symbol,effective_from,effective_to,provider_key,
                   available_at,known_at,effective_from_basis,effective_to_basis,raw)
               SELECT %(taxonomy_key)s,%(sector_key)s,t.symbol,t.effective_from,t.effective_to,%(provider_key)s,
                      %(observed_at)s,%(observed_at)s,t.effective_from_basis,t.effective_to_basis,t.raw::jsonb
                 FROM unnest(%(symbols)s::text[],%(effective_froms)s::date[],%(effective_tos)s::date[],
                              %(from_bases)s::text[],%(to_bases)s::text[],%(raws)s::text[])
                      AS t(symbol,effective_from,effective_to,effective_from_basis,effective_to_basis,raw)
               ON CONFLICT(taxonomy_key,sector_key,symbol,effective_from) DO UPDATE
                 SET effective_to=EXCLUDED.effective_to,provider_key=EXCLUDED.provider_key,
                     available_at=EXCLUDED.available_at,known_at=EXCLUDED.known_at,
                     effective_from_basis=EXCLUDED.effective_from_basis,
                     effective_to_basis=EXCLUDED.effective_to_basis,raw=EXCLUDED.raw""",
            {
                "taxonomy_key": taxonomy_key, "sector_key": sector_key, "provider_key": provider_key,
                "observed_at": observed_at,
                "symbols": [entry[0] for entry in entries],
                "effective_froms": [entry[1] for entry in entries],
                "effective_tos": [entry[2] for entry in entries],
                "from_bases": [entry[3] for entry in entries],
                "to_bases": [entry[4] for entry in entries],
                "raws": [json.dumps(entry[5], ensure_ascii=False, default=str) for entry in entries],
            },
        )
    if rows:
        connection.execute(
            """UPDATE quant.sector_membership_history
                  SET effective_to=%s,available_at=%s,known_at=%s,effective_to_basis=%s
                WHERE taxonomy_key=%s AND sector_key=%s AND provider_key=%s AND effective_to IS NULL
                  AND NOT symbol = ANY(%s)""",
            (observed_exchange_date(observed_at) - timedelta(days=1), observed_at, observed_at,
             OBSERVED_SNAPSHOT, taxonomy_key, sector_key, provider_key, list(active_members)),
        )
    return len(active_members)


def persist_observed_snapshot(
    connection: Any,
    taxonomy_key: str,
    sector_key: str,
    rows: list[dict[str, Any]],
    provider_key: str,
    observed_at: datetime,
    *,
    member_symbol: Callable[[dict[str, Any]], str | None],
    ensure_instrument: Callable[[Any, str, dict[str, Any]], None],
) -> int:
    """Store a provider snapshot that has no historical membership interval."""
    members: set[str] = set()
    stored = 0
    effective_from = observed_exchange_date(observed_at)
    # ``effective_to``/basis columns are constant for the whole snapshot, so
    # only ``symbol``/``raw`` vary per row: collected here and written in one
    # batched upsert instead of one INSERT per member.  Deduplicated by
    # symbol (last one wins) because the conflict key only varies by symbol
    # within one call, and PostgreSQL rejects an ON CONFLICT DO UPDATE that
    # would affect the same target row twice in a single statement.
    raw_by_symbol: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = member_symbol(row)
        if not symbol:
            continue
        ensure_instrument(connection, symbol, row)
        raw_by_symbol[symbol] = row
        members.add(symbol)
        stored += 1
    if raw_by_symbol:
        symbols = list(raw_by_symbol)
        connection.execute(
            """INSERT INTO quant.sector_membership_history(
                   taxonomy_key,sector_key,symbol,effective_from,effective_to,provider_key,
                   available_at,known_at,effective_from_basis,effective_to_basis,raw)
               SELECT %(taxonomy_key)s,%(sector_key)s,t.symbol,%(effective_from)s,NULL,%(provider_key)s,
                      %(observed_at)s,%(observed_at)s,%(basis)s,%(basis)s,t.raw::jsonb
                 FROM unnest(%(symbols)s::text[],%(raws)s::text[]) AS t(symbol,raw)
               ON CONFLICT(taxonomy_key,sector_key,symbol,effective_from) DO UPDATE
                 SET effective_to=NULL,provider_key=EXCLUDED.provider_key,
                     available_at=EXCLUDED.available_at,known_at=EXCLUDED.known_at,
                     effective_from_basis=EXCLUDED.effective_from_basis,
                     effective_to_basis=EXCLUDED.effective_to_basis,raw=EXCLUDED.raw""",
            {
                "taxonomy_key": taxonomy_key, "sector_key": sector_key, "provider_key": provider_key,
                "observed_at": observed_at, "effective_from": effective_from, "basis": OBSERVED_SNAPSHOT,
                "symbols": symbols,
                "raws": [json.dumps(raw_by_symbol[symbol], ensure_ascii=False, default=str) for symbol in symbols],
            },
        )
    if rows:
        connection.execute(
            """UPDATE quant.sector_membership_history
                  SET effective_to=%s,available_at=%s,known_at=%s,effective_to_basis=%s
                WHERE taxonomy_key=%s AND sector_key=%s AND provider_key=%s AND effective_to IS NULL
                  AND effective_from<%s AND NOT symbol = ANY(%s)""",
            (effective_from - timedelta(days=1), observed_at, observed_at, OBSERVED_SNAPSHOT,
             taxonomy_key, sector_key, provider_key, effective_from, list(members)),
        )
    return stored


__all__ = [
    "LEGACY_UNBOUNDED", "OBSERVED_SNAPSHOT", "PROVIDER_INTERVAL", "membership_interval",
    "observed_exchange_date", "persist_observed_snapshot", "persist_ths_snapshot",
    "point_in_time_membership_predicate",
]
