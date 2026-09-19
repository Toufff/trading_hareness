"""Derive an auditable close limit-up pool from persisted settled evidence."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timezone
from typing import Any


SOURCE = "longhuvip_composite_close_limit_derived"

#: The one predicate that decides "this symbol closed at its limit".  Both the
#: writer below and the read-only completeness probe use it, so the pool can
#: never be judged complete against a different rule than the one that filled
#: it.  ``daily_trade_limits`` holds one row per provider, so a symbol that has
#: both a licensed and a Tushare limit row joins twice: the writer collapses the
#: duplicates through ``event_identity_key`` and the probe must therefore count
#: DISTINCT symbols, not join rows.
CLOSED_AT_LIMIT_PREDICATE = """bar.trading_date=%s AND bar.close IS NOT NULL
   AND limits.limit_up IS NOT NULL AND bar.close >= limits.limit_up - 0.005"""

SETTLED_LIMIT_POOL_STATUS_SQL = f"""SELECT
     (SELECT count(DISTINCT bar.symbol)::int
        FROM quant.canonical_bars_daily bar
        JOIN quant.daily_trade_limits limits
          ON limits.symbol=bar.symbol AND limits.trading_date=bar.trading_date
       WHERE {CLOSED_AT_LIMIT_PREDICATE}) AS expected_symbols,
     (SELECT count(*)::int FROM quant.market_events
       WHERE event_identity_key LIKE %s) AS stored_symbols"""


def settled_limit_pool_query(trade_date: date) -> tuple[str, tuple]:
    """Return the read-only completeness probe for one date's close limit pool.

    The pool is *derived* from bars and limits this database already holds, so
    its expected size is knowable from the same rows rather than from the wall
    clock: whatever the vendor publishes late, a pool that is smaller than the
    bars say it should be means the ``limit_ladder`` stage has not run against
    the settled cross-section yet.
    """
    return SETTLED_LIMIT_POOL_STATUS_SQL, (
        trade_date, f"{SOURCE}:limit_up_pool:%:{trade_date.isoformat()}")


def settled_limit_pool_payload(row: Any, trade_date: date) -> dict[str, Any]:
    """Shape the probe row for the pipeline runner's late-dataset decision.

    ``stored`` may legitimately exceed ``expected`` (an upsert never deletes, so
    a revised limit price leaves yesterday's row behind); only a *short* pool is
    evidence that the stage still owes work, and the PowerShell guard decides
    that, not this function.
    """
    values = dict(row or {})
    return {
        "trade_date": trade_date.isoformat(),
        "source": SOURCE,
        "limit_pool": {
            "expected_symbols": int(values.get("expected_symbols") or 0),
            "stored_symbols": int(values.get("stored_symbols") or 0),
        },
    }


def persist_settled_limit_pool(database: Any, trade_date: date) -> dict[str, Any]:
    """Persist close-at-limit facts without claiming intraday sealing history.

    The row is eligible only when canonical close and the same-date daily
    limit price both exist.  It says the stock *closed* at its limit; it does
    not infer first-seal time, broken-board count, queue size or order origin.
    """
    with database.transaction() as connection:
        rows = connection.execute(
            f"""SELECT bar.symbol,instrument.name,bar.close,bar.high,bar.volume,bar.amount,
                      limits.limit_up,bar.available_at,
                      fundamentals.turnover_rate,fundamentals.volume_ratio
                 FROM quant.canonical_bars_daily bar
                 JOIN quant.daily_trade_limits limits
                   ON limits.symbol=bar.symbol AND limits.trading_date=bar.trading_date
                 LEFT JOIN quant.instruments instrument ON instrument.symbol=bar.symbol
                 LEFT JOIN LATERAL (
                   SELECT item.turnover_rate,item.volume_ratio
                     FROM quant.daily_fundamentals item
                    WHERE item.symbol=bar.symbol AND item.trading_date=bar.trading_date
                    ORDER BY CASE WHEN item.provider=bar.selected_provider THEN 0 ELSE 1 END,
                             item.available_at DESC LIMIT 1
                 ) fundamentals ON true
                WHERE {CLOSED_AT_LIMIT_PREDICATE}
                ORDER BY bar.symbol""",
            (trade_date,),
        ).fetchall()
        stored = 0
        for row in rows:
            symbol = str(row["symbol"])
            available_at = row["available_at"] or datetime.now(timezone.utc)
            raw = {
                "ts_code": symbol,
                "trade_date": trade_date.strftime("%Y%m%d"),
                "name": row.get("name") or symbol,
                "limit_type": "涨停池",
                "status": "收盘封板",
                "close": row.get("close"),
                "high": row.get("high"),
                "volume": row.get("volume"),
                "amount": row.get("amount"),
                "limit_price": row.get("limit_up"),
                "turnover_rate": row.get("turnover_rate"),
                "volume_ratio": row.get("volume_ratio"),
                "limit_amount": None,
                "semantic_boundary": (
                    "settled close-at-limit only; no first-seal time, break count, queue size or order identity"
                ),
            }
            body = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
            identity = f"{SOURCE}:limit_up_pool:{symbol}:{trade_date.isoformat()}"
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            connection.execute(
                """INSERT INTO quant.market_events(
                       event_id,symbol,event_type,occurred_at,available_at,source,title,body,
                       url,content_sha256,event_identity_key)
                   VALUES(%s,%s,'limit_up_pool',%s,%s,%s,%s,%s,NULL,%s,%s)
                   ON CONFLICT(event_identity_key) WHERE event_identity_key IS NOT NULL DO UPDATE SET
                     available_at=greatest(quant.market_events.available_at,EXCLUDED.available_at),
                     title=EXCLUDED.title,body=EXCLUDED.body,content_sha256=EXCLUDED.content_sha256""",
                (
                    uuid.uuid4(), symbol, available_at, available_at, SOURCE,
                    f"收盘涨停：{raw['name']}", body, digest, identity,
                ),
            )
            stored += 1
    return {
        "status": "completed",
        "trade_date": str(trade_date),
        "source": SOURCE,
        "stored": stored,
        "semantic_boundary": "settled close-at-limit only",
    }


__all__ = [
    "CLOSED_AT_LIMIT_PREDICATE", "SETTLED_LIMIT_POOL_STATUS_SQL", "SOURCE",
    "persist_settled_limit_pool", "settled_limit_pool_payload", "settled_limit_pool_query",
]
