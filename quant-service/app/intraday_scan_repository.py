"""Persistence primitives and bounded reads for one intraday scan.

The scan scheduler owns timing and provider calls; this module owns only the
small synchronous transaction used to record a blocked/empty terminal state.
Keeping the database object explicit makes the function safe to submit to the
bounded database executor and gives the remaining scan persistence a stable
extraction seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
import uuid

from psycopg.types.json import Json

from .provider_health import record_provider_failure
from .tushare_providers import safe_error_detail


@dataclass(frozen=True)
class IntradayScanLocalState:
    """Bounded local inputs used by one signal-persistence transaction."""

    order_book_by_symbol: dict[str, list[dict[str, Any]]]
    paper_positions: dict[str, dict[str, Any]]
    candidate_sector_keys: dict[str, list[str]]
    snapshot_payload: dict[str, Any]


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


def load_intraday_scan_local_state(
    connection: Any,
    selected_symbols: list[str],
    *,
    observed_at: datetime,
    session_start: datetime,
    local_trade_date: date,
) -> IntradayScanLocalState:
    """Load all bounded scan-local SQL inputs without provider access.

    The caller still owns its surrounding transaction and the immediately
    preceding portfolio snapshot write.  Keeping these reads together makes
    the scan's SQL contract testable while preserving the exact existing
    snapshot, 5-minute order-book and point-in-time membership boundaries.
    """
    order_book_rows = connection.execute(
        """SELECT symbol,observed_at,raw FROM quant.intraday_quote_observations
             WHERE symbol=ANY(%s) AND source_name='tencent_order_book'
               AND observed_at>=%s AND observed_at<%s
             ORDER BY symbol,observed_at DESC""",
        (selected_symbols, max(session_start, observed_at - timedelta(minutes=5)), observed_at),
    ).fetchall()
    order_book_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for item in order_book_rows:
        order_book_by_symbol.setdefault(str(item["symbol"]), []).append(dict(item))
    paper_positions = {
        str(row["symbol"]): dict(row)
        for row in connection.execute(
            "SELECT symbol,quantity,sellable_quantity,average_cost FROM quant.paper_positions WHERE symbol=ANY(%s)",
            (selected_symbols,),
        ).fetchall()
    }
    sector_rows = connection.execute(
        """SELECT symbol,sector_key FROM quant.sector_membership_history
            WHERE symbol=ANY(%s) AND effective_from<=%s
              AND (effective_to IS NULL OR effective_to>=%s)
              AND taxonomy_key IN ('ths_concept_flow','ths_index_n','ths_industry')""",
        (selected_symbols, local_trade_date, local_trade_date),
    ).fetchall() if selected_symbols else []
    candidate_sector_keys: dict[str, list[str]] = {}
    for row in sector_rows:
        candidate_sector_keys.setdefault(str(row["symbol"]), []).append(str(row["sector_key"]))
    paper_snapshot = connection.execute(
        "SELECT drawdown,payload FROM quant.paper_portfolio_snapshots ORDER BY as_of DESC LIMIT 1",
    ).fetchone()
    snapshot_payload = dict(paper_snapshot["payload"] or {}) if paper_snapshot else {}
    if paper_snapshot:
        snapshot_payload["drawdown"] = paper_snapshot["drawdown"]
    return IntradayScanLocalState(
        order_book_by_symbol=order_book_by_symbol,
        paper_positions=paper_positions,
        candidate_sector_keys=candidate_sector_keys,
        snapshot_payload=snapshot_payload,
    )


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


__all__ = [
    "IntradayScanLocalState",
    "first_eac_breakout_events",
    "load_intraday_scan_local_state",
    "persist_intraday_scan_terminal",
    "previous_quote_frames",
]
