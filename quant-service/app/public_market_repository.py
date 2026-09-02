"""Local persistence for public-market evidence.

This repository owns only database writes and reads for public quote, daily
evidence and market events.  It deliberately has no HTTP client, provider
selection or scheduling logic, so slow public providers remain isolated behind
the bounded executor selected by their callers.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, time, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from .analysis import as_utc
from .daily_bar_repository import exchange_for
from .market_flow_features import market_event_identity_key
from .market_rules import cn_today
from .repo_common import SYMBOL_RE

#: A same-day daily row is only accepted once the exchange session has
#: actually settled.  15:05 Asia/Shanghai is a conservative floor past the
#: 15:00 close, absorbing clock skew without accepting a still-running bar.
SESSION_SETTLED_TIME = time(15, 5)


def persist_free_quote(database: Any, provider: str, symbol: str, quote: dict[str, Any] | None) -> int:
    if not quote:
        return 0
    payload = json.dumps(quote, ensure_ascii=False, sort_keys=True, default=str)
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO quant.raw_market_observations(provider_key,capability,market,symbol,effective_at,available_at,payload_sha256,normalized,payload)
               VALUES(%s,'realtime_quote','cn',%s,now(),now(),%s,%s,%s)
               ON CONFLICT(provider_key,capability,market,symbol,effective_at,payload_sha256) DO UPDATE SET available_at=now()""",
            (provider, symbol, hashlib.sha256(payload.encode()).hexdigest(), Json(quote), Json(quote)),
        )
    return 1


def persist_free_quotes(database: Any, provider: str, quotes: list[dict[str, Any]]) -> int:
    """Write a market quote batch in one set-based statement; malformed rows are skipped.

    Previously one ``INSERT`` per quote (up to ~5,500 statements for an all-A
    snapshot).  Every row shares the same ``provider``/``effective_at`` for
    this call, so the whole batch collapses into a single ``unnest``-driven
    upsert.  Rows are deduplicated by ``(symbol, payload_sha256)`` first
    because PostgreSQL rejects an ``ON CONFLICT DO UPDATE`` that would affect
    the same target row twice within one statement.
    """
    observed_at = datetime.now(timezone.utc)
    deduplicated: dict[tuple[str, str], str] = {}
    for quote in quotes:
        symbol = str(quote.get("ts_code") or "").upper()
        if not SYMBOL_RE.fullmatch(symbol):
            continue
        payload = json.dumps(quote, ensure_ascii=False, sort_keys=True, default=str)
        content_sha256 = hashlib.sha256(payload.encode()).hexdigest()
        deduplicated[(symbol, content_sha256)] = payload
    if not deduplicated:
        return 0
    symbols = [key[0] for key in deduplicated]
    shas = [key[1] for key in deduplicated]
    payloads = list(deduplicated.values())
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO quant.raw_market_observations(provider_key,capability,market,symbol,effective_at,available_at,payload_sha256,normalized,payload)
               SELECT %(provider)s,'realtime_quote','cn',t.symbol,%(observed_at)s,%(observed_at)s,t.sha,t.payload_json::jsonb,t.payload_json::jsonb
                 FROM unnest(%(symbols)s::text[],%(shas)s::text[],%(payloads)s::text[]) AS t(symbol,sha,payload_json)
               ON CONFLICT(provider_key,capability,market,symbol,effective_at,payload_sha256) DO UPDATE SET available_at=EXCLUDED.available_at""",
            {"provider": provider, "observed_at": observed_at, "symbols": symbols, "shas": shas, "payloads": payloads},
        )
    return len(deduplicated)


def persist_public_observations(database: Any, provider: str, capability: str,
                                rows: list[dict[str, Any]], symbol: str | None = None) -> int:
    """Persist public aggregate rows as raw evidence without canonical promotion.

    One set-based upsert instead of one ``INSERT`` per row.  ``record_index``
    is folded into each row's payload before hashing, exactly as before, so a
    genuine duplicate ``(symbol, payload_sha256)`` pair only occurs for
    byte-identical rows; those are deduplicated (last one wins) to satisfy
    PostgreSQL's single-command ``ON CONFLICT`` restriction.
    """
    observed_at = datetime.now(timezone.utc)
    deduplicated: dict[tuple[str | None, str], str] = {}
    for index, row in enumerate(rows):
        row_symbol = str(row.get("ts_code") or symbol or "").upper() or None
        if row_symbol and not SYMBOL_RE.fullmatch(row_symbol):
            row_symbol = None
        payload = {**row, "provider_key": provider, "capability": capability, "record_index": index}
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        content_sha256 = hashlib.sha256(serialized.encode()).hexdigest()
        deduplicated[(row_symbol, content_sha256)] = serialized
    if not deduplicated:
        return 0
    symbols = [key[0] for key in deduplicated]
    shas = [key[1] for key in deduplicated]
    payloads = list(deduplicated.values())
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO quant.raw_market_observations(provider_key,capability,market,symbol,effective_at,available_at,payload_sha256,normalized,payload)
               SELECT %(provider)s,%(capability)s,'cn',t.symbol,%(observed_at)s,%(observed_at)s,t.sha,t.payload_json::jsonb,t.payload_json::jsonb
                 FROM unnest(%(symbols)s::text[],%(shas)s::text[],%(payloads)s::text[]) AS t(symbol,sha,payload_json)
               ON CONFLICT(provider_key,capability,market,symbol,effective_at,payload_sha256) DO UPDATE SET available_at=EXCLUDED.available_at""",
            {"provider": provider, "capability": capability, "observed_at": observed_at,
             "symbols": symbols, "shas": shas, "payloads": payloads},
        )
    return len(deduplicated)


def _record_unsettled_daily_row_issue(connection: Any, provider: str, symbol: str, trading_date: Any) -> None:
    """Record one unresolved issue per symbol/date without duplicating retries."""
    connection.execute(
        """INSERT INTO quant.data_quality_issues(capability,symbol,trading_date,severity,code,message,details)
           SELECT 'daily_bar',%s,%s,'warning','unsettled_session_daily_row',
                  'a same-day public daily row was rejected because the session had not settled',%s
            WHERE NOT EXISTS (
                SELECT 1 FROM quant.data_quality_issues
                 WHERE capability='daily_bar' AND symbol=%s AND trading_date=%s
                   AND code='unsettled_session_daily_row' AND resolved_at IS NULL
            )""",
        (symbol, trading_date, Json({"provider": provider}), symbol, trading_date),
    )


def _record_malformed_daily_rows_issue(connection: Any, provider: str, rejected_count: int) -> None:
    connection.execute(
        """INSERT INTO quant.data_quality_issues(capability,severity,code,message,details)
           VALUES('daily_bar','warning','malformed_free_daily_rows',
                  'free daily rows were skipped for a missing/invalid symbol or trading date',%s)""",
        (Json({"provider": provider, "rejected_row_count": rejected_count}),),
    )


def persist_free_daily(
    database: Any,
    provider: str,
    rows: list[dict[str, Any]],
    *,
    daily_bar_type: Any,
    parse_trade_date: Callable[[Any], Any],
    decimal_or_none: Callable[[Any], Any],
    upsert_bar: Callable[[Any, Any], None],
    persist_raw_observations: Callable[[Any, str, str, list[dict[str, Any]]], int],
    observed_at: datetime | None = None,
) -> int:
    """Promote only validated, session-settled unadjusted public daily rows.

    Tencent's adapter is explicitly front-adjusted.  Its short-window bars
    remain attributable raw evidence, never a fallback for the canonical
    unadjusted series when a licensed provider has a gap.

    A same-day row (``trading_date >= cn_today()``) fetched before the
    session has settled is a still-running intraday bar, not a public daily
    close; promoting it let a stock-study call at 09:35 write today's K-line
    into canonical hours before it was final.  ``observed_at`` is the
    injectable "now" for both this guard and the row's ``available_at``.
    """
    if provider == "tencent_free":
        return persist_raw_observations(database, provider, "daily_bar", rows)

    available_at = observed_at or datetime.now(timezone.utc)
    today = cn_today(available_at)
    session_settled = available_at.astimezone(ZoneInfo("Asia/Shanghai")).time() >= SESSION_SETTLED_TIME
    valid_bars: list[Any] = []
    unsettled_rejections: list[tuple[str, Any]] = []
    malformed_count = 0
    for row in rows:
        try:
            trading_date = parse_trade_date(row.get("trade_date"))
            symbol = str(row.get("ts_code") or "").upper()
            if not trading_date or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
                raise ValueError("free daily row is missing a valid symbol or trading date")
            if trading_date >= today and not session_settled:
                unsettled_rejections.append((symbol, trading_date))
                continue
            valid_bars.append(daily_bar_type(
                symbol=symbol, trading_date=trading_date, close=decimal_or_none(row.get("close")),
                open=decimal_or_none(row.get("open")), high=decimal_or_none(row.get("high")),
                low=decimal_or_none(row.get("low")), volume=decimal_or_none(row.get("vol")),
                amount=decimal_or_none(row.get("amount")), source=provider, available_at=available_at,
            ))
        except Exception:
            # A malformed public row must never displace licensed canonical
            # data; it is counted and recorded rather than silently dropped.
            malformed_count += 1
            continue
    if not valid_bars and not unsettled_rejections and not malformed_count:
        return 0
    with database.transaction() as connection:
        for bar in valid_bars:
            upsert_bar(connection, bar)
        for symbol, trading_date in unsettled_rejections:
            _record_unsettled_daily_row_issue(connection, provider, symbol, trading_date)
        if malformed_count:
            _record_malformed_daily_rows_issue(connection, provider, malformed_count)
    return len(valid_bars)


def _record_naive_published_at_issue(connection: Any, provider: str, symbol: str, event_type: str, raw_value: str) -> None:
    connection.execute(
        """INSERT INTO quant.data_quality_issues(capability,symbol,severity,code,message,details)
           VALUES('market_event',%s,'warning','naive_published_at_timestamp',
                  'a market event published_at had no timezone and was rejected rather than misinterpreted',%s)""",
        (symbol, Json({"provider": provider, "event_type": event_type, "raw_published_at": raw_value})),
    )


def persist_market_events(database: Any, provider: str, rows: list[dict[str, Any]]) -> int:
    """Store public event evidence without making it a hard trading signal.

    ``published_at`` must be an aware timestamp.  A naive one is ambiguous
    between local Shanghai time and UTC, and ``as_utc`` resolves that
    ambiguity by assuming UTC -- which previously turned a provider's naive
    "00:00 on trade_date" into 08:00 Shanghai, hours before a limit pool that
    only settles at the close was actually known.  Rather than silently
    guessing, a naive timestamp is rejected and recorded here.

    ``availability_basis`` (e.g. ``"post_close_publication"``) lets the
    conflict-merge below only take the earlier of two ``available_at``
    values when they were derived the same way; a real intraday capture
    must never be overwritten by a later post-close-only recompute (or vice
    versa) via a blind ``LEAST()``.
    """
    stored = 0
    with database.transaction() as connection:
        for row in rows:
            symbol = str(row.get("ts_code") or "").upper()
            title = str(row.get("title") or row.get("short_title") or "").strip()
            url = str(row.get("url") or "").strip() or None
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol) or not title:
                continue
            event_type = str(row.get("event_type") or "announcement")
            raw_published_at = row.get("published_at")
            if raw_published_at:
                parsed_published_at = datetime.fromisoformat(str(raw_published_at))
                if parsed_published_at.tzinfo is None:
                    _record_naive_published_at_issue(connection, provider, symbol, event_type, str(raw_published_at))
                    continue
                published_at = as_utc(parsed_published_at)
            else:
                published_at = datetime.now(timezone.utc)
            availability_basis = str(row.get("availability_basis") or "").strip() or None
            payload = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            content_sha256 = hashlib.sha256(payload.encode()).hexdigest()
            occurred_date = published_at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            identity_key = str(row.get("event_identity_key") or "").strip() or market_event_identity_key(
                provider, event_type, symbol, occurred_date,
            )
            connection.execute(
                "INSERT INTO quant.instruments(symbol,exchange,source) VALUES(%s,%s,%s) ON CONFLICT(symbol) DO NOTHING",
                (symbol, exchange_for(symbol), provider),
            )
            values = (
                uuid.uuid4(), symbol, event_type, published_at, published_at, provider, title,
                json.dumps(row.get("raw") or row, ensure_ascii=False, default=str), url, content_sha256, identity_key,
                availability_basis,
            )
            # available_at (and its basis) is only merged toward the earlier
            # value when both writes share the same availability_basis
            # (NULL treated as its own "unknown" basis); a write derived a
            # different way keeps the row's existing available_at untouched.
            if identity_key is not None:
                connection.execute(
                    """INSERT INTO quant.market_events(
                           event_id,symbol,event_type,occurred_at,available_at,source,title,body,url,content_sha256,
                           event_identity_key,availability_basis)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(event_identity_key) WHERE event_identity_key IS NOT NULL DO UPDATE SET
                         available_at=CASE
                           WHEN coalesce(quant.market_events.availability_basis,'unknown')=coalesce(EXCLUDED.availability_basis,'unknown')
                           THEN LEAST(quant.market_events.available_at,EXCLUDED.available_at)
                           ELSE quant.market_events.available_at END,
                         title=EXCLUDED.title,body=EXCLUDED.body,url=EXCLUDED.url,
                         content_sha256=EXCLUDED.content_sha256""",
                    values,
                )
            else:
                connection.execute(
                    """INSERT INTO quant.market_events(
                           event_id,symbol,event_type,occurred_at,available_at,source,title,body,url,content_sha256,
                           event_identity_key,availability_basis)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(content_sha256) DO UPDATE SET
                         available_at=CASE
                           WHEN coalesce(quant.market_events.availability_basis,'unknown')=coalesce(EXCLUDED.availability_basis,'unknown')
                           THEN LEAST(quant.market_events.available_at,EXCLUDED.available_at)
                           ELSE quant.market_events.available_at END,
                         title=EXCLUDED.title,body=EXCLUDED.body,url=EXCLUDED.url""",
                    values,
                )
            stored += 1
    return stored


def recent_market_events(database: Any, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT event_id,symbol,event_type,occurred_at,available_at,source,title,url,created_at
                 FROM quant.market_events WHERE symbol=%s ORDER BY occurred_at DESC,created_at DESC LIMIT %s""",
            (symbol, max(1, min(limit, 100))),
        ).fetchall()
    return [dict(row) for row in rows]


__all__ = [
    "persist_free_daily", "persist_free_quote", "persist_free_quotes", "persist_market_events",
    "persist_public_observations", "recent_market_events",
]
