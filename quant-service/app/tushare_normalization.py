"""Tushare raw-to-control-plane normalization transaction."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable
import re

from psycopg.types.json import Json


def normalize_rows(
    connection: Any, api_name: str, rows: list[dict[str, Any]], available_at: datetime,
    *,
    core_apis: set[str] | frozenset[str],
    date_parser: Callable[[Any], Any], exchange_for: Callable[[str], str],
    is_st_security_name: Callable[[Any], bool], ensure_instrument: Callable[[Any, str], None],
    upsert_bar: Callable[[Any, Any], None], daily_bar_type: Callable[..., Any],
    decimal_or_none: Callable[[Any], Any], safe_error_detail: Callable[[str, int], str],
    provider_key: str = "tushare",
) -> int:
    """Promote a deterministic subset of raw rows; preserve row-level warnings."""
    if api_name not in core_apis:
        return 0
    normalized = 0
    for row in rows:
        try:
            if api_name == "trade_cal":
                calendar_date = date_parser(row.get("cal_date"))
                if not calendar_date:
                    raise ValueError("trade_cal row has no cal_date")
                connection.execute("""INSERT INTO quant.market_trade_calendar(exchange,calendar_date,is_open,pretrade_date,provider,available_at,raw)
                       VALUES(%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(exchange,calendar_date) DO UPDATE SET is_open=EXCLUDED.is_open,pretrade_date=EXCLUDED.pretrade_date, available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
                    (str(row.get("exchange") or "SSE"), calendar_date, str(row.get("is_open")) == "1", date_parser(row.get("pretrade_date")), provider_key, available_at, Json(row)))
            elif api_name == "stock_basic":
                symbol = str(row.get("ts_code") or "").upper()
                if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
                    raise ValueError("stock_basic row has invalid ts_code")
                connection.execute("""INSERT INTO quant.instruments(symbol,exchange,name,industry,list_date,delist_date,is_st,source)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(symbol) DO UPDATE SET exchange=EXCLUDED.exchange,name=coalesce(EXCLUDED.name,quant.instruments.name), industry=coalesce(EXCLUDED.industry,quant.instruments.industry),list_date=coalesce(EXCLUDED.list_date,quant.instruments.list_date), delist_date=coalesce(EXCLUDED.delist_date,quant.instruments.delist_date),is_st=EXCLUDED.is_st, source=EXCLUDED.source,updated_at=now()""",
                    (symbol, str(row.get("exchange") or exchange_for(symbol)), row.get("name"), row.get("industry"), date_parser(row.get("list_date")), date_parser(row.get("delist_date")), is_st_security_name(row.get("name")), provider_key))
            elif api_name == "suspend_d":
                symbol = str(row.get("ts_code") or "").upper()
                suspend_date = date_parser(row.get("trade_date") or row.get("suspend_date"))
                if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol) or not suspend_date:
                    raise ValueError("suspend_d row needs ts_code and trade_date")
                ensure_instrument(connection, symbol)
                resume_date = date_parser(row.get("resume_date"))
                connection.execute("""INSERT INTO quant.security_suspensions(symbol,suspend_date,resume_date,suspend_reason,provider,available_at,raw) VALUES(%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(symbol,suspend_date,provider) DO UPDATE SET resume_date=EXCLUDED.resume_date,suspend_reason=EXCLUDED.suspend_reason, available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
                    (symbol, suspend_date, resume_date, row.get("suspend_timing") or row.get("suspend_reason"), provider_key, available_at, Json(row)))
                if resume_date:
                    connection.execute("""UPDATE quant.canonical_bars_daily SET is_suspended=true,canonicalized_at=now()
                             WHERE symbol=%s AND trading_date >= %s AND trading_date < %s""",
                        (symbol, suspend_date, resume_date))
                else:
                    # Daily cross-section responses normally describe one
                    # suspended trading day and omit ``resume_date``.  Treating
                    # that omission as an open-ended interval would mark every
                    # later bar as suspended during a historical backfill.
                    connection.execute("""UPDATE quant.canonical_bars_daily SET is_suspended=true,canonicalized_at=now()
                             WHERE symbol=%s AND trading_date=%s""", (symbol, suspend_date))
            else:
                symbol = str(row.get("ts_code") or "").upper()
                trading_date = date_parser(row.get("trade_date"))
                if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol) or not trading_date:
                    raise ValueError(f"{api_name} row needs ts_code and trade_date")
                ensure_instrument(connection, symbol)
                if api_name in {"daily", "index_daily"}:
                    upsert_bar(connection, daily_bar_type(symbol=symbol, trading_date=trading_date, open=decimal_or_none(row.get("open")), high=decimal_or_none(row.get("high")), low=decimal_or_none(row.get("low")), close=decimal_or_none(row.get("close")), pre_close=decimal_or_none(row.get("pre_close")), volume=decimal_or_none(row.get("vol")), amount=decimal_or_none(row.get("amount")), source=provider_key, available_at=available_at))
                elif api_name == "adj_factor":
                    adj_factor = decimal_or_none(row.get("adj_factor"))
                    if adj_factor is None:
                        raise ValueError("adj_factor row has no positive adj_factor")
                    connection.execute("""INSERT INTO quant.daily_adjustment_factors(symbol,trading_date,adj_factor,provider,available_at,raw) VALUES(%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(symbol,trading_date,provider) DO UPDATE SET adj_factor=EXCLUDED.adj_factor,available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""", (symbol, trading_date, adj_factor, provider_key, available_at, Json(row)))
                    connection.execute("UPDATE quant.canonical_bars_daily SET adj_factor=%s,canonicalized_at=now() WHERE symbol=%s AND trading_date=%s", (adj_factor, symbol, trading_date))
                elif api_name == "daily_basic":
                    connection.execute("""INSERT INTO quant.daily_fundamentals(symbol,trading_date,close,turnover_rate,volume_ratio,pe,pb,total_share,float_share,total_mv,circ_mv,provider,available_at,raw)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(symbol,trading_date,provider) DO UPDATE SET close=EXCLUDED.close,turnover_rate=EXCLUDED.turnover_rate, volume_ratio=EXCLUDED.volume_ratio,pe=EXCLUDED.pe,pb=EXCLUDED.pb,total_share=EXCLUDED.total_share,float_share=EXCLUDED.float_share, total_mv=EXCLUDED.total_mv,circ_mv=EXCLUDED.circ_mv,available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""", (symbol, trading_date, decimal_or_none(row.get("close")), decimal_or_none(row.get("turnover_rate")), decimal_or_none(row.get("volume_ratio")), decimal_or_none(row.get("pe")), decimal_or_none(row.get("pb")), decimal_or_none(row.get("total_share")), decimal_or_none(row.get("float_share")), decimal_or_none(row.get("total_mv")), decimal_or_none(row.get("circ_mv")), provider_key, available_at, Json(row)))
                elif api_name == "stk_limit":
                    up, down = decimal_or_none(row.get("up_limit")), decimal_or_none(row.get("down_limit"))
                    connection.execute("""INSERT INTO quant.daily_trade_limits(symbol,trading_date,limit_up,limit_down,provider,available_at,raw) VALUES(%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(symbol,trading_date,provider) DO UPDATE SET limit_up=EXCLUDED.limit_up,limit_down=EXCLUDED.limit_down, available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""", (symbol, trading_date, up, down, provider_key, available_at, Json(row)))
                    connection.execute("UPDATE quant.canonical_bars_daily SET limit_up=%s,limit_down=%s,canonicalized_at=now() WHERE symbol=%s AND trading_date=%s", (up, down, symbol, trading_date))
            normalized += 1
        except Exception as error:
            connection.execute("""INSERT INTO quant.data_quality_issues(capability,severity,code,message,details)
                   VALUES(%s,'warning','tushare_normalization_failed',%s,%s)""", (api_name, safe_error_detail(str(error), 500), Json({"row": row})))
    return normalized


__all__ = ["normalize_rows"]
