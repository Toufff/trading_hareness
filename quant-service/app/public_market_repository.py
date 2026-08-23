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
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from .analysis import as_utc
from .daily_bar_repository import exchange_for
from .market_flow_features import market_event_identity_key


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
    """Write a market quote batch in one transaction; malformed rows are skipped."""
    accepted = 0
    observed_at = datetime.now(timezone.utc)
    with database.transaction() as connection:
        for quote in quotes:
            symbol = str(quote.get("ts_code") or "").upper()
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
                continue
            payload = json.dumps(quote, ensure_ascii=False, sort_keys=True, default=str)
            connection.execute(
                """INSERT INTO quant.raw_market_observations(provider_key,capability,market,symbol,effective_at,available_at,payload_sha256,normalized,payload)
                   VALUES(%s,'realtime_quote','cn',%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(provider_key,capability,market,symbol,effective_at,payload_sha256) DO UPDATE SET available_at=EXCLUDED.available_at""",
                (provider, symbol, observed_at, observed_at, hashlib.sha256(payload.encode()).hexdigest(), Json(quote), Json(quote)),
            )
            accepted += 1
    return accepted


def persist_public_observations(database: Any, provider: str, capability: str,
                                rows: list[dict[str, Any]], symbol: str | None = None) -> int:
    """Persist public aggregate rows as raw evidence without canonical promotion."""
    observed_at = datetime.now(timezone.utc)
    accepted = 0
    with database.transaction() as connection:
        for index, row in enumerate(rows):
            row_symbol = str(row.get("ts_code") or symbol or "").upper() or None
            if row_symbol and not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", row_symbol):
                row_symbol = None
            payload = {**row, "provider_key": provider, "capability": capability, "record_index": index}
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            connection.execute(
                """INSERT INTO quant.raw_market_observations(provider_key,capability,market,symbol,effective_at,available_at,payload_sha256,normalized,payload)
                   VALUES(%s,%s,'cn',%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(provider_key,capability,market,symbol,effective_at,payload_sha256) DO UPDATE SET available_at=EXCLUDED.available_at""",
                (provider, capability, row_symbol, observed_at, observed_at, hashlib.sha256(serialized.encode()).hexdigest(), Json(payload), Json(payload)),
            )
            accepted += 1
    return accepted


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
    """Promote only validated unadjusted public daily rows in one transaction.

    Tencent's adapter is explicitly front-adjusted.  Its short-window bars
    remain attributable raw evidence, never a fallback for the canonical
    unadjusted series when a licensed provider has a gap.
    """
    if provider == "tencent_free":
        return persist_raw_observations(database, provider, "daily_bar", rows)

    available_at = observed_at or datetime.now(timezone.utc)
    valid_bars: list[Any] = []
    for row in rows:
        try:
            trading_date = parse_trade_date(row.get("trade_date"))
            symbol = str(row.get("ts_code") or "").upper()
            if not trading_date or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
                raise ValueError("free daily row is missing a valid symbol or trading date")
            valid_bars.append(daily_bar_type(
                symbol=symbol, trading_date=trading_date, close=decimal_or_none(row.get("close")),
                open=decimal_or_none(row.get("open")), high=decimal_or_none(row.get("high")),
                low=decimal_or_none(row.get("low")), volume=decimal_or_none(row.get("vol")),
                amount=decimal_or_none(row.get("amount")), source=provider, available_at=available_at,
            ))
        except Exception:
            # The caller records source health.  A malformed public row must
            # never displace licensed canonical data.
            continue
    if not valid_bars:
        return 0
    with database.transaction() as connection:
        for bar in valid_bars:
            upsert_bar(connection, bar)
    return len(valid_bars)


def persist_market_events(database: Any, provider: str, rows: list[dict[str, Any]]) -> int:
    """Store public event evidence without making it a hard trading signal."""
    stored = 0
    with database.transaction() as connection:
        for row in rows:
            symbol = str(row.get("ts_code") or "").upper()
            title = str(row.get("title") or row.get("short_title") or "").strip()
            url = str(row.get("url") or "").strip() or None
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol) or not title:
                continue
            published_at = as_utc(datetime.fromisoformat(str(row["published_at"]))) if row.get("published_at") else datetime.now(timezone.utc)
            payload = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            content_sha256 = hashlib.sha256(payload.encode()).hexdigest()
            event_type = str(row.get("event_type") or "announcement")
            occurred_date = published_at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            identity_key = market_event_identity_key(provider, event_type, symbol, occurred_date)
            connection.execute(
                "INSERT INTO quant.instruments(symbol,exchange,source) VALUES(%s,%s,%s) ON CONFLICT(symbol) DO NOTHING",
                (symbol, exchange_for(symbol), provider),
            )
            values = (
                uuid.uuid4(), symbol, event_type, published_at, published_at, provider, title,
                json.dumps(row.get("raw") or row, ensure_ascii=False, default=str), url, content_sha256, identity_key,
            )
            if identity_key is not None:
                connection.execute(
                    """INSERT INTO quant.market_events(
                           event_id,symbol,event_type,occurred_at,available_at,source,title,body,url,content_sha256,event_identity_key)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(event_identity_key) WHERE event_identity_key IS NOT NULL DO UPDATE SET
                         available_at=LEAST(quant.market_events.available_at,EXCLUDED.available_at),
                         title=EXCLUDED.title,body=EXCLUDED.body,url=EXCLUDED.url,
                         content_sha256=EXCLUDED.content_sha256""",
                    values,
                )
            else:
                connection.execute(
                    """INSERT INTO quant.market_events(
                           event_id,symbol,event_type,occurred_at,available_at,source,title,body,url,content_sha256,event_identity_key)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(content_sha256) DO UPDATE SET
                         available_at=LEAST(quant.market_events.available_at,EXCLUDED.available_at),
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
