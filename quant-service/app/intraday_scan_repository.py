"""Persistence primitives and bounded reads for one intraday scan.

The scan scheduler owns timing and provider calls; this module owns only the
small synchronous transaction used to record a blocked/empty terminal state.
Keeping the database object explicit makes the function safe to submit to the
bounded database executor and gives the remaining scan persistence a stable
extraction seam.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

from psycopg.types.json import Json

from .provider_health import record_provider_failure
from .tushare_providers import safe_error_detail


def previous_quote_frames(
    connection: Any,
    quote_sources: dict[str, str],
    *,
    not_before: datetime,
    observed_at: datetime,
) -> dict[str, dict[str, Any]]:
    """Load each watch's most recent same-source frame in one bounded query.

    The caller supplies the actual source selected for this scan, so a Tencent
    frame can never be compared with a Sina fallback merely because both have
    the same symbol.  The existing 15-second/session boundary remains owned by
    the scanner and is passed in as ``not_before``.
    """
    pairs = sorted(
        (str(symbol), str(source))
        for symbol, source in quote_sources.items()
        if str(symbol) and str(source)
    )
    if not pairs:
        return {}
    symbols, sources = zip(*pairs)
    rows = connection.execute(
        """SELECT DISTINCT ON(o.symbol,o.source_name) o.symbol,o.source_name,o.price,o.observed_at
             FROM quant.intraday_quote_observations o
             JOIN unnest(%s::text[],%s::text[]) AS wanted(symbol,source_name)
               ON wanted.symbol=o.symbol AND wanted.source_name=o.source_name
            WHERE o.observed_at<%s AND o.observed_at>=%s
            ORDER BY o.symbol,o.source_name,o.observed_at DESC""",
        (list(symbols), list(sources), observed_at, not_before),
    ).fetchall()
    return {str(row["symbol"]): dict(row) for row in rows}


def first_eac_breakout_events(
    connection: Any,
    symbols: list[str],
    *,
    not_before: datetime,
) -> dict[str, dict[str, Any]]:
    """Read each symbol's first EAC watch event in one confirmation window."""
    normalized = sorted({str(symbol) for symbol in symbols if str(symbol)})
    if not normalized:
        return {}
    rows = connection.execute(
        """SELECT DISTINCT ON(symbol) symbol,observed_at,conditions
             FROM quant.intraday_signal_events
            WHERE symbol=ANY(%s)
              AND signal_key=symbol || ':watch:upside_breakout_eac_v3'
              AND observed_at>=%s
            ORDER BY symbol,observed_at ASC""",
        (normalized, not_before),
    ).fetchall()
    return {str(row["symbol"]): dict(row) for row in rows}


def persist_intraday_scan_terminal(
    database: Any,
    scan_id: uuid.UUID,
    observed_at: datetime,
    status: str,
    requested_symbols: list[str],
    source_status: dict[str, Any],
    summary: dict[str, Any],
    provider_failure: str | None = None,
    provider_latency_ms: int | None = None,
) -> None:
    """Write a terminal scan state and optional public-source failure.

    ``provider_latency_ms`` is optional for compatibility with non-provider
    terminal states (for example a closed-session gate).  When a provider
    failed, preserving the measured elapsed time prevents the health panel
    from losing the last useful latency sample.
    """
    with database.transaction() as connection:
        if provider_failure:
            record_provider_failure(
                connection,
                "tencent_free",
                "realtime_quote",
                safe_error_detail(provider_failure, 300),
                provider_latency_ms,
            )
        connection.execute(
            """INSERT INTO quant.intraday_scan_runs(
                   scan_id,observed_at,status,requested_symbols,source_status,summary
               ) VALUES(%s,%s,%s,%s,%s,%s)""",
            (
                scan_id,
                observed_at,
                status,
                Json(requested_symbols),
                Json(source_status),
                Json(summary),
            ),
        )


__all__ = ["first_eac_breakout_events", "persist_intraday_scan_terminal", "previous_quote_frames"]
