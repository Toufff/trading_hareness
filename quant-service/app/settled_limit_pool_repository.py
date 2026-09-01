"""Derive an auditable close limit-up pool from persisted settled evidence."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timezone
from typing import Any


SOURCE = "longhuvip_composite_close_limit_derived"


def persist_settled_limit_pool(database: Any, trade_date: date) -> dict[str, Any]:
    """Persist close-at-limit facts without claiming intraday sealing history.

    The row is eligible only when canonical close and the same-date daily
    limit price both exist.  It says the stock *closed* at its limit; it does
    not infer first-seal time, broken-board count, queue size or order origin.
    """
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT bar.symbol,instrument.name,bar.close,bar.high,bar.volume,bar.amount,
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
                WHERE bar.trading_date=%s AND bar.close IS NOT NULL
                  AND limits.limit_up IS NOT NULL
                  AND bar.close >= limits.limit_up - 0.005
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


__all__ = ["SOURCE", "persist_settled_limit_pool"]
