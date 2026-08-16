"""Resumable one-year daily/control/sector evidence backfill.

This command deliberately excludes every minute and realtime capability.  It
uses one full-market request per open day, records a durable ``fetch_runs``
checkpoint for every provider/API/date, and promotes only schema-backed daily
datasets.  Specialty per-stock datasets (chip distributions, factor packs and
stock money-flow histories) are intentionally outside this bounded baseline.

Run inside the quant container, for example::

    python -m app.annual_daily_backfill --start-date 2025-08-15 --end-date 2026-08-14
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

from psycopg.types.json import Json, Jsonb

from .database import Database
from .sector_flow_repository import rebuild_sector_flow_daily_features
from .tushare_providers import ProviderCallError, call_provider, provider_configs, safe_error_detail


# Exchange suffix alone is not enough: stk_limit also returns funds and other
# six-digit securities.  Keep the canonical full-A path to listed-share code
# families while retaining every supplier row in the raw evidence table.
STOCK_CODE = re.compile(
    r"^(?:(?:60[0135]|68[89])\d{3}\.SH|(?:000|001|002|003|300|301)\d{3}\.SZ|[489]\d{5}\.BJ)$"
)
INDEX_CODES = (
    "000001.SH", "399001.SZ", "399006.SZ", "000300.SH",
    "000852.SH", "000905.SH", "000688.SH",
)
MAX_CALENDAR_DAYS = 370


@dataclass(frozen=True)
class ApiSpec:
    api_name: str
    provider_name: str
    minimum_rows: int = 0
    legal_empty: bool = False
    promote: str = "raw"


CORE_DAILY_SPECS = (
    ApiSpec("daily", "primary", 4_800, promote="daily"),
    ApiSpec("adj_factor", "primary", 4_800, promote="adj_factor"),
    ApiSpec("daily_basic", "primary", 4_800, promote="daily_basic"),
    ApiSpec("stk_limit", "primary", 4_800, promote="stk_limit"),
    ApiSpec("suspend_d", "primary", legal_empty=True, promote="suspend_d"),
)

SECTOR_EVENT_SPECS = (
    ApiSpec("moneyflow_ind_ths", "super_sdk", 50, promote="industry_flow"),
    ApiSpec("moneyflow_cnt_ths", "super_sdk", 300, promote="concept_flow"),
    ApiSpec("limit_cpt_list", "super_sdk", legal_empty=True, promote="limit_strength"),
    ApiSpec("limit_list_ths", "super_sdk", legal_empty=True),
    ApiSpec("limit_step", "super_sdk", legal_empty=True),
    ApiSpec("top_list", "super_sdk", legal_empty=True),
    ApiSpec("top_inst", "super_sdk", legal_empty=True),
)


def parse_iso_date(value: str) -> date:
    return date.fromisoformat(value)


def validate_range(start_date: date, end_date: date) -> None:
    if end_date < start_date:
        raise ValueError("end-date must not be before start-date")
    if (end_date - start_date).days > MAX_CALENDAR_DAYS:
        raise ValueError(f"daily backfill is capped at {MAX_CALENDAR_DAYS} calendar days")


def valid_rows(api_name: str, rows: list[dict[str, Any]], trade_date: date | None = None) -> list[dict[str, Any]]:
    """Apply only deterministic shape/date filters; never invent missing rows."""
    stamp = trade_date.strftime("%Y%m%d") if trade_date else None
    if api_name in {"daily", "adj_factor", "daily_basic", "stk_limit", "suspend_d"}:
        return [
            dict(row) for row in rows
            if STOCK_CODE.fullmatch(str(row.get("ts_code") or "").upper())
            and (stamp is None or str(row.get("trade_date") or "") == stamp)
        ]
    if api_name == "moneyflow_ind_ths":
        return [
            dict(row) for row in rows
            if str(row.get("ts_code") or "").endswith(".TI") and row.get("industry")
            and (stamp is None or str(row.get("trade_date") or "") == stamp)
        ]
    if api_name in {"moneyflow_cnt_ths", "limit_cpt_list"}:
        return [
            dict(row) for row in rows
            if str(row.get("ts_code") or "").endswith(".TI") and row.get("name")
            and (stamp is None or str(row.get("trade_date") or "") == stamp)
        ]
    return [dict(row) for row in rows]


def request_key(provider_key: str, api_name: str, params: dict[str, Any]) -> str:
    material = json.dumps(
        {"job": "annual_daily_backfill_v1", "provider": provider_key, "api": api_name, "params": params},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _stage_rows(connection: Any, rows: list[dict[str, Any]]) -> None:
    connection.execute(
        "CREATE TEMP TABLE annual_daily_stage(record_index integer NOT NULL,row_data jsonb NOT NULL) ON COMMIT DROP"
    )
    if not rows:
        return
    with connection.cursor().copy("COPY annual_daily_stage(record_index,row_data) FROM STDIN") as copy:
        for index, row in enumerate(rows):
            copy.write_row((index, Jsonb(row)))


def _persist_raw(
    connection: Any, provider_key: str, api_name: str, key: str, available_at: datetime,
) -> None:
    connection.execute(
        """INSERT INTO quant.tushare_raw_records(
               provider_key,api_name,request_key,record_index,record_key,content_sha256,row_data,available_at)
           SELECT %s,%s,%s,record_index,
                  concat_ws(':',%s::text,
                    coalesce(row_data->>'ts_code',row_data->>'con_code',row_data->>'cal_date','row'),
                    coalesce(row_data->>'trade_date',row_data->>'ann_date',row_data->>'cal_date','na'),
                    coalesce(row_data->>'exalter',row_data->>'name',row_data->>'reason',record_index::text)),
                  encode(digest(row_data::text,'sha256'),'hex'),row_data,%s
             FROM annual_daily_stage
           ON CONFLICT(provider_key,api_name,record_key,content_sha256) DO UPDATE
             SET available_at=EXCLUDED.available_at,request_key=EXCLUDED.request_key""",
        (provider_key, api_name, key, api_name, available_at),
    )


def _persist_instruments_from_stage(connection: Any, provider_key: str) -> None:
    connection.execute(
        """INSERT INTO quant.instruments(symbol,exchange,source)
           SELECT DISTINCT upper(row_data->>'ts_code'),
                  CASE right(upper(row_data->>'ts_code'),2)
                    WHEN 'SH' THEN 'SSE' WHEN 'SZ' THEN 'SZSE' ELSE 'BSE' END,
                  %s
             FROM annual_daily_stage
            WHERE upper(row_data->>'ts_code') ~ '^\\d{6}\\.(SH|SZ|BJ)$'
           ON CONFLICT(symbol) DO NOTHING""",
        (provider_key,),
    )


def _persist_daily(connection: Any, provider_key: str, available_at: datetime, *, index_mode: bool = False) -> None:
    if index_mode:
        connection.execute(
            """INSERT INTO quant.instruments(symbol,exchange,source)
               SELECT DISTINCT upper(row_data->>'ts_code'),
                      CASE right(upper(row_data->>'ts_code'),2) WHEN 'SH' THEN 'SSE' ELSE 'SZSE' END,%s
                 FROM annual_daily_stage
                WHERE upper(row_data->>'ts_code') ~ '^\\d{6}\\.(SH|SZ)$'
               ON CONFLICT(symbol) DO NOTHING""",
            (provider_key,),
        )
    else:
        _persist_instruments_from_stage(connection, provider_key)
    capability = "index_daily" if index_mode else "daily"
    connection.execute(
        """INSERT INTO quant.raw_market_observations(
               provider_key,capability,symbol,effective_at,available_at,payload_sha256,normalized,payload)
           SELECT %s,%s,upper(row_data->>'ts_code'),
                  (to_date(row_data->>'trade_date','YYYYMMDD') + time '15:00') AT TIME ZONE 'Asia/Shanghai',
                  %s,encode(digest(row_data::text,'sha256'),'hex'),row_data,row_data
             FROM annual_daily_stage
            WHERE row_data->>'trade_date' ~ '^\\d{8}$'
           ON CONFLICT(provider_key,capability,market,symbol,effective_at,payload_sha256) DO UPDATE
             SET available_at=EXCLUDED.available_at,normalized=EXCLUDED.normalized,payload=EXCLUDED.payload""",
        (provider_key, capability, available_at),
    )
    connection.execute(
        """WITH parsed AS (
               SELECT upper(s.row_data->>'ts_code') symbol,
                      to_date(s.row_data->>'trade_date','YYYYMMDD') trading_date,
                      nullif(s.row_data->>'open','')::numeric open,
                      nullif(s.row_data->>'high','')::numeric high,
                      nullif(s.row_data->>'low','')::numeric low,
                      nullif(s.row_data->>'close','')::numeric close,
                      nullif(s.row_data->>'pre_close','')::numeric pre_close,
                      nullif(s.row_data->>'vol','')::numeric volume,
                      nullif(s.row_data->>'amount','')::numeric amount,
                      encode(digest(s.row_data::text,'sha256'),'hex') payload_sha256
                 FROM annual_daily_stage s
                WHERE s.row_data->>'trade_date' ~ '^\\d{8}$'
                  AND nullif(s.row_data->>'close','') IS NOT NULL
             ), with_observation AS (
               SELECT p.*,o.observation_id
                 FROM parsed p JOIN quant.raw_market_observations o
                   ON o.provider_key=%s AND o.capability=%s AND o.symbol=p.symbol
                  AND o.effective_at=(p.trading_date + time '15:00') AT TIME ZONE 'Asia/Shanghai'
                  AND o.payload_sha256=p.payload_sha256
             )
           INSERT INTO quant.market_bars_daily(
               symbol,trading_date,open,high,low,close,pre_close,volume,amount,source,available_at)
           SELECT symbol,trading_date,open,high,low,close,pre_close,volume,amount,%s,%s
             FROM with_observation
           ON CONFLICT(symbol,trading_date) DO UPDATE SET
             open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,close=EXCLUDED.close,
             pre_close=EXCLUDED.pre_close,volume=EXCLUDED.volume,amount=EXCLUDED.amount,
             source=EXCLUDED.source,available_at=EXCLUDED.available_at""",
        (provider_key, capability, provider_key, available_at),
    )
    connection.execute(
        """WITH parsed AS (
               SELECT upper(s.row_data->>'ts_code') symbol,
                      to_date(s.row_data->>'trade_date','YYYYMMDD') trading_date,
                      nullif(s.row_data->>'open','')::numeric open,
                      nullif(s.row_data->>'high','')::numeric high,
                      nullif(s.row_data->>'low','')::numeric low,
                      nullif(s.row_data->>'close','')::numeric close,
                      nullif(s.row_data->>'pre_close','')::numeric pre_close,
                      nullif(s.row_data->>'vol','')::numeric volume,
                      nullif(s.row_data->>'amount','')::numeric amount,
                      encode(digest(s.row_data::text,'sha256'),'hex') payload_sha256
                 FROM annual_daily_stage s
                WHERE s.row_data->>'trade_date' ~ '^\\d{8}$'
                  AND nullif(s.row_data->>'close','') IS NOT NULL
             ), with_observation AS (
               SELECT p.*,o.observation_id
                 FROM parsed p JOIN quant.raw_market_observations o
                   ON o.provider_key=%s AND o.capability=%s AND o.symbol=p.symbol
                  AND o.effective_at=(p.trading_date + time '15:00') AT TIME ZONE 'Asia/Shanghai'
                  AND o.payload_sha256=p.payload_sha256
             )
           INSERT INTO quant.canonical_bars_daily(
               symbol,trading_date,open,high,low,close,pre_close,volume,amount,selected_provider,
               source_observation_ids,quality_status,available_at)
           SELECT symbol,trading_date,open,high,low,close,pre_close,volume,amount,%s,
                  array[observation_id],'fresh',%s
             FROM with_observation
           ON CONFLICT(symbol,trading_date) DO UPDATE SET
             open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,close=EXCLUDED.close,
             pre_close=EXCLUDED.pre_close,volume=EXCLUDED.volume,amount=EXCLUDED.amount,
             selected_provider=EXCLUDED.selected_provider,
             source_observation_ids=EXCLUDED.source_observation_ids,
             quality_status='fresh',available_at=EXCLUDED.available_at,canonicalized_at=now()""",
        (provider_key, capability, provider_key, available_at),
    )


def _persist_adj_factor(connection: Any, provider_key: str, available_at: datetime) -> None:
    _persist_instruments_from_stage(connection, provider_key)
    connection.execute(
        """INSERT INTO quant.daily_adjustment_factors(symbol,trading_date,adj_factor,provider,available_at,raw)
           SELECT upper(row_data->>'ts_code'),to_date(row_data->>'trade_date','YYYYMMDD'),
                  nullif(row_data->>'adj_factor','')::numeric,%s,%s,row_data
             FROM annual_daily_stage
            WHERE row_data->>'trade_date' ~ '^\\d{8}$' AND nullif(row_data->>'adj_factor','') IS NOT NULL
           ON CONFLICT(symbol,trading_date,provider) DO UPDATE SET
             adj_factor=EXCLUDED.adj_factor,available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
        (provider_key, available_at),
    )
    for table in ("market_bars_daily", "canonical_bars_daily"):
        connection.execute(
            f"""UPDATE quant.{table} bar SET adj_factor=nullif(stage.row_data->>'adj_factor','')::numeric
                  FROM annual_daily_stage stage
                 WHERE bar.symbol=upper(stage.row_data->>'ts_code')
                   AND bar.trading_date=to_date(stage.row_data->>'trade_date','YYYYMMDD')"""
        )


def _persist_daily_basic(connection: Any, provider_key: str, available_at: datetime) -> None:
    _persist_instruments_from_stage(connection, provider_key)
    connection.execute(
        """INSERT INTO quant.daily_fundamentals(
               symbol,trading_date,close,turnover_rate,volume_ratio,pe,pb,total_share,float_share,total_mv,circ_mv,
               provider,available_at,raw)
           SELECT upper(row_data->>'ts_code'),to_date(row_data->>'trade_date','YYYYMMDD'),
                  nullif(row_data->>'close','')::numeric,nullif(row_data->>'turnover_rate','')::numeric,
                  nullif(row_data->>'volume_ratio','')::numeric,nullif(row_data->>'pe','')::numeric,
                  nullif(row_data->>'pb','')::numeric,nullif(row_data->>'total_share','')::numeric,
                  nullif(row_data->>'float_share','')::numeric,nullif(row_data->>'total_mv','')::numeric,
                  nullif(row_data->>'circ_mv','')::numeric,%s,%s,row_data
             FROM annual_daily_stage WHERE row_data->>'trade_date' ~ '^\\d{8}$'
           ON CONFLICT(symbol,trading_date,provider) DO UPDATE SET
             close=EXCLUDED.close,turnover_rate=EXCLUDED.turnover_rate,volume_ratio=EXCLUDED.volume_ratio,
             pe=EXCLUDED.pe,pb=EXCLUDED.pb,total_share=EXCLUDED.total_share,float_share=EXCLUDED.float_share,
             total_mv=EXCLUDED.total_mv,circ_mv=EXCLUDED.circ_mv,available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
        (provider_key, available_at),
    )


def _persist_stk_limit(connection: Any, provider_key: str, available_at: datetime) -> None:
    _persist_instruments_from_stage(connection, provider_key)
    connection.execute(
        """INSERT INTO quant.daily_trade_limits(symbol,trading_date,limit_up,limit_down,provider,available_at,raw)
           SELECT upper(row_data->>'ts_code'),to_date(row_data->>'trade_date','YYYYMMDD'),
                  nullif(row_data->>'up_limit','')::numeric,nullif(row_data->>'down_limit','')::numeric,%s,%s,row_data
             FROM annual_daily_stage WHERE row_data->>'trade_date' ~ '^\\d{8}$'
           ON CONFLICT(symbol,trading_date,provider) DO UPDATE SET
             limit_up=EXCLUDED.limit_up,limit_down=EXCLUDED.limit_down,
             available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
        (provider_key, available_at),
    )
    for table in ("market_bars_daily", "canonical_bars_daily"):
        connection.execute(
            f"""UPDATE quant.{table} bar
                   SET limit_up=nullif(stage.row_data->>'up_limit','')::numeric,
                       limit_down=nullif(stage.row_data->>'down_limit','')::numeric
                  FROM annual_daily_stage stage
                 WHERE bar.symbol=upper(stage.row_data->>'ts_code')
                   AND bar.trading_date=to_date(stage.row_data->>'trade_date','YYYYMMDD')"""
        )


def _persist_suspend_d(connection: Any, provider_key: str, available_at: datetime) -> None:
    _persist_instruments_from_stage(connection, provider_key)
    connection.execute(
        """INSERT INTO quant.security_suspensions(
               symbol,suspend_date,resume_date,suspend_reason,provider,available_at,raw)
           SELECT upper(row_data->>'ts_code'),to_date(row_data->>'trade_date','YYYYMMDD'),
                  CASE WHEN row_data->>'resume_date' ~ '^\\d{8}$'
                       THEN to_date(row_data->>'resume_date','YYYYMMDD') END,
                  coalesce(row_data->>'suspend_timing',row_data->>'suspend_reason'),%s,%s,row_data
             FROM annual_daily_stage WHERE row_data->>'trade_date' ~ '^\\d{8}$'
           ON CONFLICT(symbol,suspend_date,provider) DO UPDATE SET
             resume_date=EXCLUDED.resume_date,suspend_reason=EXCLUDED.suspend_reason,
             available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
        (provider_key, available_at),
    )
    connection.execute(
        """UPDATE quant.canonical_bars_daily bar SET is_suspended=true,canonicalized_at=now()
              FROM annual_daily_stage stage
             WHERE bar.symbol=upper(stage.row_data->>'ts_code')
               AND ((stage.row_data->>'resume_date' ~ '^\\d{8}$'
                     AND bar.trading_date>=to_date(stage.row_data->>'trade_date','YYYYMMDD')
                     AND bar.trading_date<to_date(stage.row_data->>'resume_date','YYYYMMDD'))
                    OR (coalesce(stage.row_data->>'resume_date','') !~ '^\\d{8}$'
                        AND bar.trading_date=to_date(stage.row_data->>'trade_date','YYYYMMDD')))"""
    )


def _persist_trade_calendar(connection: Any, provider_key: str, available_at: datetime) -> None:
    connection.execute(
        """INSERT INTO quant.market_trade_calendar(exchange,calendar_date,is_open,pretrade_date,provider,available_at,raw)
           SELECT coalesce(nullif(row_data->>'exchange',''),'SSE'),to_date(row_data->>'cal_date','YYYYMMDD'),
                  row_data->>'is_open'='1',
                  CASE WHEN row_data->>'pretrade_date' ~ '^\\d{8}$'
                       THEN to_date(row_data->>'pretrade_date','YYYYMMDD') END,
                  %s,%s,row_data
             FROM annual_daily_stage WHERE row_data->>'cal_date' ~ '^\\d{8}$'
           ON CONFLICT(exchange,calendar_date) DO UPDATE SET
             is_open=EXCLUDED.is_open,pretrade_date=EXCLUDED.pretrade_date,provider=EXCLUDED.provider,
             available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
        (provider_key, available_at),
    )


def _persist_stock_basic(connection: Any, provider_key: str) -> None:
    connection.execute(
        """INSERT INTO quant.instruments(symbol,exchange,name,industry,list_date,delist_date,is_st,source)
           SELECT upper(row_data->>'ts_code'),
                  coalesce(nullif(row_data->>'exchange',''),
                    CASE right(upper(row_data->>'ts_code'),2)
                      WHEN 'SH' THEN 'SSE' WHEN 'SZ' THEN 'SZSE' ELSE 'BSE' END),
                  nullif(row_data->>'name',''),nullif(row_data->>'industry',''),
                  CASE WHEN row_data->>'list_date' ~ '^\\d{8}$' THEN to_date(row_data->>'list_date','YYYYMMDD') END,
                  CASE WHEN row_data->>'delist_date' ~ '^\\d{8}$' THEN to_date(row_data->>'delist_date','YYYYMMDD') END,
                  coalesce(row_data->>'name','') ~* '(^|\\*)ST',%s
             FROM annual_daily_stage
            WHERE upper(row_data->>'ts_code') ~ '^\\d{6}\\.(SH|SZ|BJ)$'
           ON CONFLICT(symbol) DO UPDATE SET
             exchange=EXCLUDED.exchange,name=coalesce(EXCLUDED.name,quant.instruments.name),
             industry=coalesce(EXCLUDED.industry,quant.instruments.industry),
             list_date=coalesce(EXCLUDED.list_date,quant.instruments.list_date),
             delist_date=coalesce(EXCLUDED.delist_date,quant.instruments.delist_date),
             is_st=EXCLUDED.is_st,source=EXCLUDED.source,updated_at=now()""",
        (provider_key,),
    )


def _persist_sector_flow(
    connection: Any, provider_key: str, available_at: datetime, *, kind: str,
) -> None:
    if kind == "industry_flow":
        taxonomy_key, label, name_field, close_field = "ths_industry", "同花顺行业", "industry", "close"
    elif kind == "concept_flow":
        taxonomy_key, label, name_field, close_field = "ths_concept_flow", "同花顺概念资金流", "name", "industry_index"
    else:
        taxonomy_key, label, name_field, close_field = "ths_limit_strength", "同花顺概念涨停强度", "name", ""
    connection.execute(
        """INSERT INTO quant.sector_taxonomies(taxonomy_key,label,provider_key,metadata)
           VALUES(%s,%s,%s,%s)
           ON CONFLICT(taxonomy_key) DO UPDATE SET
             label=EXCLUDED.label,provider_key=EXCLUDED.provider_key,metadata=EXCLUDED.metadata,updated_at=now()""",
        (taxonomy_key, label, provider_key, Json({"backfill": "annual_daily_backfill_v1"})),
    )
    connection.execute(
        f"""INSERT INTO quant.sectors(taxonomy_key,sector_key,label,metadata)
            SELECT %s,row_data->>'ts_code',row_data->>%s::text,
                   jsonb_build_object(%s::text,row_data->>%s::text)
              FROM annual_daily_stage
             WHERE row_data->>'ts_code' LIKE '%%.TI' AND nullif(row_data->>%s::text,'') IS NOT NULL
            ON CONFLICT(taxonomy_key,sector_key) DO UPDATE SET
              label=EXCLUDED.label,metadata=EXCLUDED.metadata,updated_at=now()""",
        (taxonomy_key, name_field, name_field, name_field, name_field),
    )
    if kind == "limit_strength":
        connection.execute(
            """INSERT INTO quant.sector_market_observations(
                   taxonomy_key,sector_key,trading_date,provider_key,available_at,change_pct,constituent_count,raw)
               SELECT %s,row_data->>'ts_code',to_date(row_data->>'trade_date','YYYYMMDD'),%s,%s,
                      nullif(row_data->>'pct_chg','')::numeric,
                      nullif(row_data->>'cons_nums','')::integer,row_data
                 FROM annual_daily_stage WHERE row_data->>'trade_date' ~ '^\\d{8}$'
               ON CONFLICT(taxonomy_key,sector_key,trading_date,provider_key) DO UPDATE SET
                 available_at=EXCLUDED.available_at,change_pct=EXCLUDED.change_pct,
                 constituent_count=EXCLUDED.constituent_count,raw=EXCLUDED.raw""",
            (taxonomy_key, provider_key, available_at),
        )
        return
    connection.execute(
        f"""INSERT INTO quant.sector_market_observations(
               taxonomy_key,sector_key,trading_date,provider_key,available_at,close,change_pct,
               net_amount,net_buy_amount,net_sell_amount,constituent_count,leading_label,raw)
           SELECT %s,row_data->>'ts_code',to_date(row_data->>'trade_date','YYYYMMDD'),%s,%s,
                  nullif(row_data->>%s::text,'')::numeric,nullif(row_data->>'pct_change','')::numeric,
                  nullif(row_data->>'net_amount','')::numeric,nullif(row_data->>'net_buy_amount','')::numeric,
                  nullif(row_data->>'net_sell_amount','')::numeric,
                  nullif(row_data->>'company_num','')::integer,row_data->>'lead_stock',row_data
             FROM annual_daily_stage WHERE row_data->>'trade_date' ~ '^\\d{8}$'
           ON CONFLICT(taxonomy_key,sector_key,trading_date,provider_key) DO UPDATE SET
             available_at=EXCLUDED.available_at,close=EXCLUDED.close,change_pct=EXCLUDED.change_pct,
             net_amount=EXCLUDED.net_amount,net_buy_amount=EXCLUDED.net_buy_amount,
             net_sell_amount=EXCLUDED.net_sell_amount,constituent_count=EXCLUDED.constituent_count,
             leading_label=EXCLUDED.leading_label,raw=EXCLUDED.raw""",
        (taxonomy_key, provider_key, available_at, close_field),
    )


PROMOTERS: dict[str, Callable[..., None]] = {
    "daily": _persist_daily,
    "adj_factor": _persist_adj_factor,
    "daily_basic": _persist_daily_basic,
    "stk_limit": _persist_stk_limit,
    "suspend_d": _persist_suspend_d,
}


class AnnualDailyBackfill:
    def __init__(self, database: Database, start_date: date, end_date: date) -> None:
        validate_range(start_date, end_date)
        self.db = database
        self.start_date = start_date
        self.end_date = end_date
        self.providers = provider_configs()
        self.failures: list[dict[str, Any]] = []
        self.counts: dict[str, int] = {}

    def _prepare_run(self, provider_key: str, api_name: str, params: dict[str, Any], day: date | None) -> tuple[str, bool]:
        key = request_key(provider_key, api_name, params)
        with self.db.transaction() as connection:
            prior = connection.execute(
                "SELECT status FROM quant.fetch_runs WHERE request_key=%s", (key,),
            ).fetchone()
            if prior and prior["status"] == "completed":
                return key, True
            connection.execute(
                """INSERT INTO quant.fetch_runs(
                       provider_key,capability,trade_date,request_key,status,attempt_count,started_at,metadata)
                   VALUES(%s,%s,%s,%s,'running',1,now(),%s)
                   ON CONFLICT(request_key) DO UPDATE SET
                     status='running',attempt_count=quant.fetch_runs.attempt_count+1,started_at=now(),
                     finished_at=null,error_class=null,error_message=null,metadata=EXCLUDED.metadata""",
                (provider_key, f"annual:{api_name}", day, key, Json({"params": params, "minute_data": False})),
            )
        return key, False

    def _finish_run(self, key: str, rows: int) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE quant.fetch_runs SET status='completed',row_count=%s,finished_at=now() WHERE request_key=%s",
                (rows, key),
            )

    def _fail_run(self, key: str, error: Exception) -> None:
        detail = safe_error_detail(str(error), 800)
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE quant.fetch_runs SET status='failed',finished_at=now(),
                     error_class=%s,error_message=%s WHERE request_key=%s""",
                (type(error).__name__, detail, key),
            )

    def _store(
        self, provider_key: str, api_name: str, key: str, rows: list[dict[str, Any]], promote: str,
    ) -> None:
        available_at = datetime.now(timezone.utc)
        with self.db.transaction() as connection:
            _stage_rows(connection, rows)
            _persist_raw(connection, provider_key, api_name, key, available_at)
            if promote in PROMOTERS:
                PROMOTERS[promote](connection, provider_key, available_at)
            elif promote in {"industry_flow", "concept_flow", "limit_strength"}:
                _persist_sector_flow(connection, provider_key, available_at, kind=promote)

    async def fetch_one(
        self, spec: ApiSpec, params: dict[str, Any], *, day: date | None = None,
        row_filter: bool = True,
    ) -> str:
        provider = self.providers[spec.provider_name]
        key, skip = self._prepare_run(provider.key, spec.api_name, params, day)
        if skip:
            return "skipped"
        try:
            rows = await call_provider(provider, spec.api_name, params, None)
            filtered = valid_rows(spec.api_name, rows, day) if row_filter else [dict(row) for row in rows]
            if len(filtered) < spec.minimum_rows:
                raise ProviderCallError(
                    f"{spec.api_name} returned {len(filtered)} valid rows; expected at least {spec.minimum_rows}"
                )
            if not filtered and not spec.legal_empty and spec.minimum_rows == 0:
                raise ProviderCallError(f"{spec.api_name} returned an unexpected empty response")
            self._store(provider.key, spec.api_name, key, filtered, spec.promote)
            self._finish_run(key, len(filtered))
            self.counts[spec.api_name] = self.counts.get(spec.api_name, 0) + len(filtered)
            return "completed"
        except Exception as error:  # noqa: BLE001 - durable failure ledger is intentional
            self._fail_run(key, error)
            self.failures.append({
                "api_name": spec.api_name, "trade_date": str(day) if day else None,
                "provider": provider.key, "error": safe_error_detail(str(error), 300),
            })
            return "failed"

    async def bootstrap(self) -> list[date]:
        primary = self.providers["primary"]
        calendar_params = {
            "exchange": "SSE", "start_date": self.start_date.strftime("%Y%m%d"),
            "end_date": self.end_date.strftime("%Y%m%d"),
        }
        calendar_spec = ApiSpec("trade_cal", "primary", 300, promote="raw")
        key, skip = self._prepare_run(primary.key, "trade_cal", calendar_params, None)
        if not skip:
            try:
                rows = await call_provider(primary, "trade_cal", calendar_params, None)
                if len(rows) < calendar_spec.minimum_rows:
                    raise ProviderCallError(f"trade_cal returned only {len(rows)} rows")
                available_at = datetime.now(timezone.utc)
                with self.db.transaction() as connection:
                    _stage_rows(connection, rows)
                    _persist_raw(connection, primary.key, "trade_cal", key, available_at)
                    _persist_trade_calendar(connection, primary.key, available_at)
                self._finish_run(key, len(rows))
            except Exception as error:
                self._fail_run(key, error)
                raise

        for status in ("L", "D", "P"):
            params = {"exchange": "", "list_status": status}
            stock_key, stock_skip = self._prepare_run(primary.key, "stock_basic", params, None)
            if stock_skip:
                continue
            try:
                rows = await call_provider(primary, "stock_basic", params, None)
                rows = [dict(row, _list_status=status) for row in rows if STOCK_CODE.fullmatch(str(row.get("ts_code") or "").upper())]
                if status == "L" and len(rows) < 4_800:
                    raise ProviderCallError(f"stock_basic active universe returned only {len(rows)} rows")
                available_at = datetime.now(timezone.utc)
                with self.db.transaction() as connection:
                    _stage_rows(connection, rows)
                    _persist_raw(connection, primary.key, "stock_basic", stock_key, available_at)
                    _persist_stock_basic(connection, primary.key)
                self._finish_run(stock_key, len(rows))
            except Exception as error:
                self._fail_run(stock_key, error)
                raise

        with self.db.transaction() as connection:
            rows = connection.execute(
                """SELECT calendar_date FROM quant.market_trade_calendar
                    WHERE exchange='SSE' AND is_open AND calendar_date BETWEEN %s AND %s
                    ORDER BY calendar_date""",
                (self.start_date, self.end_date),
            ).fetchall()
        days = [row["calendar_date"] for row in rows]
        if len(days) < 220:
            raise RuntimeError(f"calendar produced only {len(days)} open days")
        return days

    async def core_lane(self, days: list[date]) -> None:
        for index, day in enumerate(days, start=1):
            stamp = day.strftime("%Y%m%d")
            for spec in CORE_DAILY_SPECS:
                await self.fetch_one(spec, {"trade_date": stamp}, day=day)
            if index == 1 or index % 10 == 0 or index == len(days):
                print(json.dumps({"lane": "core", "day": str(day), "progress": f"{index}/{len(days)}", "failures": len(self.failures)}), flush=True)

    async def sector_lane(self, days: list[date]) -> None:
        for index, day in enumerate(days, start=1):
            stamp = day.strftime("%Y%m%d")
            for spec in SECTOR_EVENT_SPECS:
                await self.fetch_one(spec, {"trade_date": stamp}, day=day)
            if index == 1 or index % 10 == 0 or index == len(days):
                print(json.dumps({"lane": "sector", "day": str(day), "progress": f"{index}/{len(days)}", "failures": len(self.failures)}), flush=True)

    async def index_lane(self) -> None:
        spec = ApiSpec("index_daily", "primary", 200, promote="raw")
        provider = self.providers["primary"]
        for symbol in INDEX_CODES:
            params = {
                "ts_code": symbol, "start_date": self.start_date.strftime("%Y%m%d"),
                "end_date": self.end_date.strftime("%Y%m%d"),
            }
            key, skip = self._prepare_run(provider.key, "index_daily", params, None)
            if skip:
                continue
            try:
                rows = await call_provider(provider, "index_daily", params, None)
                if len(rows) < spec.minimum_rows:
                    raise ProviderCallError(f"index_daily {symbol} returned only {len(rows)} rows")
                available_at = datetime.now(timezone.utc)
                with self.db.transaction() as connection:
                    _stage_rows(connection, rows)
                    _persist_raw(connection, provider.key, "index_daily", key, available_at)
                    _persist_daily(connection, provider.key, available_at, index_mode=True)
                self._finish_run(key, len(rows))
                self.counts["index_daily"] = self.counts.get("index_daily", 0) + len(rows)
            except Exception as error:
                self._fail_run(key, error)
                self.failures.append({"api_name": "index_daily", "symbol": symbol, "error": safe_error_detail(str(error), 300)})

    def reconcile_suspensions(self) -> None:
        """Repair historical open-ended marks after all daily suspension rows exist."""
        with self.db.transaction() as connection:
            for table in ("market_bars_daily", "canonical_bars_daily"):
                connection.execute(
                    f"UPDATE quant.{table} SET is_suspended=false WHERE trading_date BETWEEN %s AND %s",
                    (self.start_date, self.end_date),
                )
                connection.execute(
                    f"""UPDATE quant.{table} bar SET is_suspended=true
                          FROM quant.security_suspensions suspension
                         WHERE bar.symbol=suspension.symbol AND bar.trading_date BETWEEN %s AND %s
                           AND ((suspension.resume_date IS NULL AND bar.trading_date=suspension.suspend_date)
                             OR (suspension.resume_date IS NOT NULL
                                 AND bar.trading_date>=suspension.suspend_date
                                 AND bar.trading_date<suspension.resume_date))""",
                    (self.start_date, self.end_date),
                )

    def rebuild_sector_features(self) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        cursor = self.start_date
        while cursor <= self.end_date:
            chunk_end = min(self.end_date, date.fromordinal(cursor.toordinal() + 44))
            results.append(rebuild_sector_flow_daily_features(self.db, cursor, chunk_end))
            cursor = date.fromordinal(chunk_end.toordinal() + 1)
        return {
            "chunks": len(results), "stored": sum(int(item.get("stored") or 0) for item in results),
            "last_outcomes": results[-1].get("outcomes") if results else None,
        }

    async def run(self) -> dict[str, Any]:
        days = await self.bootstrap()
        print(json.dumps({
            "status": "started", "start_date": str(self.start_date), "end_date": str(self.end_date),
            "open_days": len(days), "minute_data": False,
        }), flush=True)
        await asyncio.gather(self.core_lane(days), self.sector_lane(days), self.index_lane())
        self.reconcile_suspensions()
        feature_result = self.rebuild_sector_features()
        with self.db.transaction() as connection:
            coverage = connection.execute(
                """SELECT
                     count(*) FILTER (WHERE trading_date BETWEEN %s AND %s)::bigint daily_rows,
                     count(DISTINCT trading_date) FILTER (WHERE trading_date BETWEEN %s AND %s)::int daily_days,
                     count(*) FILTER (WHERE trading_date BETWEEN %s AND %s AND adj_factor IS NOT NULL)::bigint adjusted_rows,
                     count(*) FILTER (WHERE trading_date BETWEEN %s AND %s AND limit_up IS NOT NULL)::bigint limited_rows
                   FROM quant.canonical_bars_daily""",
                (self.start_date, self.end_date, self.start_date, self.end_date,
                 self.start_date, self.end_date, self.start_date, self.end_date),
            ).fetchone()
        return {
            "status": "partial" if self.failures else "completed",
            "start_date": str(self.start_date), "end_date": str(self.end_date),
            "open_days": len(days), "minute_data": False, "row_counts": self.counts,
            "coverage": dict(coverage), "sector_features": feature_result,
            "failure_count": len(self.failures), "failures": self.failures[:50],
        }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Backfill one bounded year of non-minute China market data")
    command.add_argument("--start-date", required=True, type=parse_iso_date)
    command.add_argument("--end-date", required=True, type=parse_iso_date)
    return command


async def async_main() -> int:
    args = parser().parse_args()
    database = Database()
    try:
        result = await AnnualDailyBackfill(database, args.start_date, args.end_date).run()
        print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
        return 0 if result["status"] == "completed" else 2
    finally:
        database.close()


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()


__all__ = [
    "ApiSpec", "AnnualDailyBackfill", "CORE_DAILY_SPECS", "SECTOR_EVENT_SPECS",
    "INDEX_CODES", "request_key", "valid_rows", "validate_range",
]
