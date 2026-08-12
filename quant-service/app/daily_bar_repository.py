"""Canonical A-share daily-bar persistence, isolated from HTTP orchestration.

The functions here only operate on a caller-owned transaction or repository.
They do not own a provider client, invoke a market endpoint, or decide when a
sync should run.  Keeping raw evidence, canonical selection and quality
warnings together protects the P0 price-basis/control-plane invariants while
making the same write contract reusable by future offline replay.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from psycopg.types.json import Json

from .analysis import as_utc
from .request_models import DailyBar


def exchange_for(symbol: str) -> str:
    return symbol.rsplit(".", 1)[1]


def provider_priority(provider: str) -> int:
    return {
        "tushare": 10, "tushare_primary": 10, "tushare_super_get": 15,
        "tushare_super_sdk": 20, "tushare_super": 25,
        "baostock": 30, "tushare_backup": 40, "eastmoney_free": 45,
        "akshare": 50, "tencent_free": 50, "sina_free": 55, "manual": 90,
    }.get(provider, 80)


def upsert_daily_bar(connection: Any, bar: DailyBar) -> None:
    """Persist one licensed/unadjusted daily bar and its immutable evidence."""
    if bar.source == "tencent_free":
        # Tencent's public adapter is qfq/front-adjusted.  It can be retained
        # as raw research evidence but must never enter the unadjusted series.
        raise ValueError("tencent_free front-adjusted daily rows are raw research evidence only")
    connection.execute(
        """INSERT INTO quant.instruments(symbol,exchange,name,industry,is_st,source)
           VALUES(%s,%s,%s,%s,coalesce(%s,false),%s)
           ON CONFLICT(symbol) DO UPDATE SET exchange=EXCLUDED.exchange,
              name=coalesce(EXCLUDED.name,quant.instruments.name),
              industry=coalesce(EXCLUDED.industry,quant.instruments.industry),
              is_st=CASE WHEN %s::boolean IS NULL THEN quant.instruments.is_st ELSE EXCLUDED.is_st END,
              source=EXCLUDED.source, updated_at=now()""",
        (bar.symbol, exchange_for(bar.symbol), bar.name, bar.industry, bar.is_st, bar.source, bar.is_st),
    )
    connection.execute(
        """INSERT INTO quant.market_bars_daily(symbol,trading_date,open,high,low,close,pre_close,volume,amount,adj_factor,is_suspended,limit_up,limit_down,source,available_at)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,coalesce(%s,false),%s,%s,%s,%s)
           ON CONFLICT(symbol,trading_date) DO UPDATE SET open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
             close=EXCLUDED.close,pre_close=EXCLUDED.pre_close,volume=EXCLUDED.volume,amount=EXCLUDED.amount,
             adj_factor=coalesce(EXCLUDED.adj_factor,quant.market_bars_daily.adj_factor),
             is_suspended=CASE WHEN %s::boolean IS NULL THEN quant.market_bars_daily.is_suspended ELSE EXCLUDED.is_suspended END,
             limit_up=coalesce(EXCLUDED.limit_up,quant.market_bars_daily.limit_up),
             limit_down=coalesce(EXCLUDED.limit_down,quant.market_bars_daily.limit_down),source=EXCLUDED.source,available_at=EXCLUDED.available_at""",
        (bar.symbol, bar.trading_date, bar.open, bar.high, bar.low, bar.close, bar.pre_close, bar.volume,
         bar.amount, bar.adj_factor, bar.is_suspended, bar.limit_up, bar.limit_down, bar.source,
         as_utc(bar.available_at), bar.is_suspended),
    )
    normalized = bar.model_dump(mode="json")
    payload_sha256 = hashlib.sha256(repr(sorted(normalized.items())).encode("utf-8")).hexdigest()
    observation = connection.execute(
        """INSERT INTO quant.raw_market_observations(provider_key,capability,market,symbol,effective_at,available_at,payload_sha256,normalized,payload)
           VALUES(%s,'daily_bar','cn',%s,%s,%s,%s,%s,%s)
           ON CONFLICT(provider_key,capability,market,symbol,effective_at,payload_sha256) DO UPDATE SET available_at=EXCLUDED.available_at
           RETURNING observation_id""",
        (bar.source, bar.symbol, datetime.combine(bar.trading_date, datetime.min.time(), tzinfo=timezone.utc), as_utc(bar.available_at),
         payload_sha256, Json(normalized), Json(normalized)),
    ).fetchone()
    existing = connection.execute(
        "SELECT close,selected_provider,source_observation_ids FROM quant.canonical_bars_daily WHERE symbol=%s AND trading_date=%s",
        (bar.symbol, bar.trading_date),
    ).fetchone()
    if existing and existing["close"] and abs(Decimal(existing["close"]) - bar.close) > Decimal("0.001"):
        connection.execute(
            """INSERT INTO quant.data_quality_issues(capability,symbol,trading_date,severity,code,message,details)
               VALUES('daily_bar',%s,%s,'warning','provider_close_conflict','daily close differs across providers',%s)""",
            (bar.symbol, bar.trading_date, Json({"existing_provider": existing["selected_provider"], "existing_close": str(existing["close"]), "incoming_provider": bar.source, "incoming_close": str(bar.close)})),
        )
    replace = existing is None or provider_priority(bar.source) <= provider_priority(str(existing["selected_provider"]))
    selected_provider = bar.source if replace else str(existing["selected_provider"])
    source_ids = ([str(observation["observation_id"])] if not existing else [str(value) for value in (existing["source_observation_ids"] or [])] + [str(observation["observation_id"])])
    if replace:
        connection.execute(
            """INSERT INTO quant.canonical_bars_daily(symbol,trading_date,open,high,low,close,pre_close,volume,amount,adj_factor,is_suspended,limit_up,limit_down,
                 selected_provider,source_observation_ids,quality_status,available_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,coalesce(%s,false),%s,%s,%s,%s,'fresh',%s)
               ON CONFLICT(symbol,trading_date) DO UPDATE SET open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,close=EXCLUDED.close,
                 pre_close=EXCLUDED.pre_close,volume=EXCLUDED.volume,amount=EXCLUDED.amount,
                 adj_factor=coalesce(EXCLUDED.adj_factor,quant.canonical_bars_daily.adj_factor),
                 is_suspended=CASE WHEN %s::boolean IS NULL THEN quant.canonical_bars_daily.is_suspended ELSE EXCLUDED.is_suspended END,
                 limit_up=coalesce(EXCLUDED.limit_up,quant.canonical_bars_daily.limit_up),
                 limit_down=coalesce(EXCLUDED.limit_down,quant.canonical_bars_daily.limit_down),selected_provider=EXCLUDED.selected_provider,
                 source_observation_ids=EXCLUDED.source_observation_ids,quality_status=EXCLUDED.quality_status,available_at=EXCLUDED.available_at,canonicalized_at=now()""",
             (bar.symbol, bar.trading_date, bar.open, bar.high, bar.low, bar.close, bar.pre_close, bar.volume, bar.amount, bar.adj_factor,
              bar.is_suspended, bar.limit_up, bar.limit_down, selected_provider, source_ids, as_utc(bar.available_at),
              bar.is_suspended),
        )
    else:
        connection.execute("UPDATE quant.canonical_bars_daily SET source_observation_ids=%s,canonicalized_at=now() WHERE symbol=%s AND trading_date=%s", (source_ids, bar.symbol, bar.trading_date))


__all__ = ["exchange_for", "provider_priority", "upsert_daily_bar"]
