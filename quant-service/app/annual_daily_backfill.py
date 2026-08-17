"""Resumable one-year daily/control/sector evidence backfill.

This command deliberately excludes every minute and realtime capability.  It
uses one full-market request per open day, records a durable ``fetch_runs``
checkpoint for every provider/API/date, and promotes only schema-backed daily
datasets.  Specialty per-stock datasets (chip distributions, factor packs and
stock money-flow histories) are intentionally outside this bounded baseline.

Run inside the quant container only after an operator has explicitly approved
the historical provider traffic, for example::

    python -m app.annual_daily_backfill --start-date 2025-08-15 --end-date 2026-08-14 \
      --confirm-historical-backfill I_CONFIRM_HISTORICAL_BACKFILL
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from psycopg.types.json import Json, Jsonb

from .database import Database
from .sector_flow_repository import rebuild_sector_flow_daily_features
from .tushare_providers import ProviderCallError, call_provider, provider_configs, safe_error_detail
from .universe_history import rebuild_historical_membership_from_canonical


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
HISTORICAL_BACKFILL_CONFIRMATION = "I_CONFIRM_HISTORICAL_BACKFILL"
# Exact vendor publication time is not reconstructible from a later bulk
# download. Use a conservative, explicit post-close assumption for close-daily
# facts; preserve the actual import time separately. No minute replay may call
# this a vendor-recorded availability timestamp.
HISTORICAL_DAILY_AVAILABILITY_BASIS = "assumed_eod_1700_asia_shanghai_v1"
INGESTED_AVAILABILITY_BASIS = "provider_received_at_import_v1"


@dataclass(frozen=True)
class ApiSpec:
    api_name: str
    provider_name: str
    minimum_rows: int = 0
    legal_empty: bool = False
    promote: str = "raw"
    fallback_provider_name: str | None = None
    fallback_provider_names: tuple[str, ...] = ()

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            name for name in (self.provider_name, self.fallback_provider_name, *self.fallback_provider_names) if name
        ))


CORE_DAILY_SPECS = (
    # The verified Super GET route is the canonical daily-price gateway.  A
    # full primary response remains a per-request fallback, rather than a
    # reason to silently move daily bars back to the old SDK/primary path.
    ApiSpec("daily", "super_get", 4_800, promote="daily", fallback_provider_name="super_sdk",
            fallback_provider_names=("primary",)),
    ApiSpec("adj_factor", "super_sdk", 4_800, promote="adj_factor", fallback_provider_name="primary"),
    ApiSpec("daily_basic", "super_sdk", 4_800, promote="daily_basic", fallback_provider_name="primary"),
    ApiSpec("stk_limit", "super_sdk", 4_800, promote="stk_limit", fallback_provider_name="primary"),
    ApiSpec("suspend_d", "super_sdk", legal_empty=True, promote="suspend_d", fallback_provider_name="primary"),
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


def validate_historical_backfill_confirmation(value: str | None) -> None:
    """Refuse a costly historical pull unless the CLI explicitly acknowledges it.

    The normal service never calls this module.  This guard exists because the
    command can otherwise turn a copied one-line example into hundreds of
    provider requests and several GiB of raw evidence.  It is deliberately a
    CLI acknowledgement rather than an environment default so a stale compose
    file or inherited shell variable cannot silently authorize a backfill.
    """
    if value != HISTORICAL_BACKFILL_CONFIRMATION:
        raise ValueError(
            "historical backfill is disabled by default; pass "
            f"--confirm-historical-backfill {HISTORICAL_BACKFILL_CONFIRMATION} after approval"
        )


def historical_daily_strategy_available_at(trade_date: date) -> datetime:
    """Return the declared conservative strategy clock for daily observations."""
    return datetime.combine(trade_date, time(17, 0), tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)


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
    ingested_at: datetime, availability_basis: str,
) -> None:
    connection.execute(
        """WITH prepared AS (
               SELECT record_index,
                  concat_ws(':',%s::text,
                    coalesce(row_data->>'ts_code',row_data->>'con_code',row_data->>'cal_date','row'),
                    coalesce(row_data->>'trade_date',row_data->>'ann_date',row_data->>'cal_date','na'),
                    coalesce(row_data->>'exalter',row_data->>'name',row_data->>'reason',record_index::text)) AS record_key,
                  encode(digest(row_data::text,'sha256'),'hex') AS content_sha256,row_data
             FROM annual_daily_stage
             ), deduplicated AS (
               SELECT DISTINCT ON(record_key,content_sha256)
                      record_index,record_key,content_sha256,row_data
                 FROM prepared ORDER BY record_key,content_sha256,record_index
             )
           INSERT INTO quant.tushare_raw_records(
               provider_key,api_name,request_key,record_index,record_key,content_sha256,row_data,available_at,
               ingested_at,availability_basis)
           SELECT %s,%s,%s,record_index,record_key,content_sha256,row_data,%s,%s,%s
             FROM deduplicated
           ON CONFLICT(provider_key,api_name,record_key,content_sha256) DO UPDATE
             SET available_at=least(quant.tushare_raw_records.available_at,EXCLUDED.available_at),
                 ingested_at=coalesce(quant.tushare_raw_records.ingested_at,quant.tushare_raw_records.available_at),
                 availability_basis=EXCLUDED.availability_basis""",
        (api_name, provider_key, api_name, key, available_at, ingested_at, availability_basis),
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


def _persist_daily(connection: Any, provider_key: str, available_at: datetime, ingested_at: datetime,
                   availability_basis: str, *, index_mode: bool = False) -> None:
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
        """WITH stage AS (
               SELECT DISTINCT ON(
                          upper(row_data->>'ts_code'),row_data->>'trade_date',
                          encode(digest(row_data::text,'sha256'),'hex')
                      ) row_data
                 FROM annual_daily_stage
                WHERE row_data->>'trade_date' ~ '^\\d{8}$'
                ORDER BY upper(row_data->>'ts_code'),row_data->>'trade_date',
                         encode(digest(row_data::text,'sha256'),'hex'),record_index DESC
           ) INSERT INTO quant.raw_market_observations(
               provider_key,capability,symbol,effective_at,available_at,ingested_at,availability_basis,
               payload_sha256,normalized,payload)
           SELECT %s,%s,upper(row_data->>'ts_code'),
                  (to_date(row_data->>'trade_date','YYYYMMDD') + time '15:00') AT TIME ZONE 'Asia/Shanghai',
                  %s,%s,%s,encode(digest(row_data::text,'sha256'),'hex'),row_data,row_data
             FROM stage
           ON CONFLICT(provider_key,capability,market,symbol,effective_at,payload_sha256) DO UPDATE
             SET available_at=least(quant.raw_market_observations.available_at,EXCLUDED.available_at),
                 ingested_at=coalesce(quant.raw_market_observations.ingested_at,quant.raw_market_observations.created_at),
                 availability_basis=EXCLUDED.availability_basis,
                 normalized=EXCLUDED.normalized,payload=EXCLUDED.payload""",
        (provider_key, capability, available_at, ingested_at, availability_basis),
    )
    connection.execute(
        """WITH parsed AS (
               SELECT DISTINCT ON (upper(s.row_data->>'ts_code'),to_date(s.row_data->>'trade_date','YYYYMMDD'))
                      upper(s.row_data->>'ts_code') symbol,
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
                -- Historical gateway repairs can retain an older, shorter
                -- copy plus a later enriched copy of the same exchange bar.
                -- Raw evidence keeps both payload hashes; the canonical bar
                -- must deterministically select one so an UPSERT never
                -- attempts to affect the same (symbol, date) twice.
                ORDER BY upper(s.row_data->>'ts_code'),to_date(s.row_data->>'trade_date','YYYYMMDD'),s.record_index DESC
             ), with_observation AS (
               SELECT p.*,o.observation_id,o.available_at
                 FROM parsed p JOIN quant.raw_market_observations o
                   ON o.provider_key=%s AND o.capability=%s AND o.symbol=p.symbol
                  AND o.effective_at=(p.trading_date + time '15:00') AT TIME ZONE 'Asia/Shanghai'
                  AND o.payload_sha256=p.payload_sha256
             )
           INSERT INTO quant.market_bars_daily(
               symbol,trading_date,open,high,low,close,pre_close,volume,amount,source,available_at)
           SELECT symbol,trading_date,open,high,low,close,pre_close,volume,amount,%s,available_at
             FROM with_observation
           ON CONFLICT(symbol,trading_date) DO UPDATE SET
             open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,close=EXCLUDED.close,
             pre_close=EXCLUDED.pre_close,volume=EXCLUDED.volume,amount=EXCLUDED.amount,
             source=EXCLUDED.source,available_at=EXCLUDED.available_at""",
        (provider_key, capability, provider_key),
    )
    connection.execute(
        """WITH parsed AS (
               SELECT DISTINCT ON (upper(s.row_data->>'ts_code'),to_date(s.row_data->>'trade_date','YYYYMMDD'))
                      upper(s.row_data->>'ts_code') symbol,
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
                ORDER BY upper(s.row_data->>'ts_code'),to_date(s.row_data->>'trade_date','YYYYMMDD'),s.record_index DESC
             ), with_observation AS (
               SELECT p.*,o.observation_id,o.available_at
                 FROM parsed p JOIN quant.raw_market_observations o
                   ON o.provider_key=%s AND o.capability=%s AND o.symbol=p.symbol
                  AND o.effective_at=(p.trading_date + time '15:00') AT TIME ZONE 'Asia/Shanghai'
                  AND o.payload_sha256=p.payload_sha256
             )
           INSERT INTO quant.canonical_bars_daily(
               symbol,trading_date,open,high,low,close,pre_close,volume,amount,selected_provider,
               source_observation_ids,quality_status,available_at)
           SELECT symbol,trading_date,open,high,low,close,pre_close,volume,amount,%s,
                  array[observation_id],'fresh',available_at
             FROM with_observation
           ON CONFLICT(symbol,trading_date) DO UPDATE SET
             open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,close=EXCLUDED.close,
             pre_close=EXCLUDED.pre_close,volume=EXCLUDED.volume,amount=EXCLUDED.amount,
             selected_provider=EXCLUDED.selected_provider,
             source_observation_ids=EXCLUDED.source_observation_ids,
             quality_status='fresh',available_at=EXCLUDED.available_at,canonicalized_at=now()""",
        (provider_key, capability, provider_key),
    )


def _persist_adj_factor(connection: Any, provider_key: str, available_at: datetime, _ingested_at: datetime,
                        _availability_basis: str) -> None:
    _persist_instruments_from_stage(connection, provider_key)
    connection.execute(
        """WITH stage AS (
               SELECT DISTINCT ON(upper(row_data->>'ts_code'),row_data->>'trade_date') row_data
                 FROM annual_daily_stage WHERE row_data->>'trade_date' ~ '^\\d{8}$'
                ORDER BY upper(row_data->>'ts_code'),row_data->>'trade_date',record_index DESC
           ) INSERT INTO quant.daily_adjustment_factors(symbol,trading_date,adj_factor,provider,available_at,raw)
           SELECT upper(row_data->>'ts_code'),to_date(row_data->>'trade_date','YYYYMMDD'),
                  nullif(row_data->>'adj_factor','')::numeric,%s,%s,row_data
             FROM stage WHERE nullif(row_data->>'adj_factor','') IS NOT NULL
           ON CONFLICT(symbol,trading_date,provider) DO UPDATE SET
             adj_factor=EXCLUDED.adj_factor,available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
        (provider_key, available_at),
    )
    for table in ("market_bars_daily", "canonical_bars_daily"):
        connection.execute(
            f"""WITH stage AS (
                   SELECT DISTINCT ON(upper(row_data->>'ts_code'),row_data->>'trade_date') row_data
                     FROM annual_daily_stage WHERE row_data->>'trade_date' ~ '^\\d{{8}}$'
                    ORDER BY upper(row_data->>'ts_code'),row_data->>'trade_date',record_index DESC
               ) UPDATE quant.{table} bar SET adj_factor=nullif(stage.row_data->>'adj_factor','')::numeric
                  FROM stage
                 WHERE bar.symbol=upper(stage.row_data->>'ts_code')
                   AND bar.trading_date=to_date(stage.row_data->>'trade_date','YYYYMMDD')"""
        )


def _persist_daily_basic(connection: Any, provider_key: str, available_at: datetime, _ingested_at: datetime,
                         _availability_basis: str) -> None:
    _persist_instruments_from_stage(connection, provider_key)
    connection.execute(
        """WITH stage AS (
               SELECT DISTINCT ON(upper(row_data->>'ts_code'),row_data->>'trade_date') row_data
                 FROM annual_daily_stage WHERE row_data->>'trade_date' ~ '^\\d{8}$'
                ORDER BY upper(row_data->>'ts_code'),row_data->>'trade_date',record_index DESC
           ) INSERT INTO quant.daily_fundamentals(
               symbol,trading_date,close,turnover_rate,volume_ratio,pe,pb,total_share,float_share,total_mv,circ_mv,
               provider,available_at,raw)
           SELECT upper(row_data->>'ts_code'),to_date(row_data->>'trade_date','YYYYMMDD'),
                  nullif(row_data->>'close','')::numeric,nullif(row_data->>'turnover_rate','')::numeric,
                  nullif(row_data->>'volume_ratio','')::numeric,nullif(row_data->>'pe','')::numeric,
                  nullif(row_data->>'pb','')::numeric,nullif(row_data->>'total_share','')::numeric,
                  nullif(row_data->>'float_share','')::numeric,nullif(row_data->>'total_mv','')::numeric,
                  nullif(row_data->>'circ_mv','')::numeric,%s,%s,row_data
             FROM stage
           ON CONFLICT(symbol,trading_date,provider) DO UPDATE SET
             close=EXCLUDED.close,turnover_rate=EXCLUDED.turnover_rate,volume_ratio=EXCLUDED.volume_ratio,
             pe=EXCLUDED.pe,pb=EXCLUDED.pb,total_share=EXCLUDED.total_share,float_share=EXCLUDED.float_share,
             total_mv=EXCLUDED.total_mv,circ_mv=EXCLUDED.circ_mv,available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
        (provider_key, available_at),
    )


def _persist_stk_limit(connection: Any, provider_key: str, available_at: datetime, _ingested_at: datetime,
                       _availability_basis: str) -> None:
    _persist_instruments_from_stage(connection, provider_key)
    connection.execute(
        """WITH stage AS (
               SELECT DISTINCT ON(upper(row_data->>'ts_code'),row_data->>'trade_date') row_data
                 FROM annual_daily_stage WHERE row_data->>'trade_date' ~ '^\\d{8}$'
                ORDER BY upper(row_data->>'ts_code'),row_data->>'trade_date',record_index DESC
           ) INSERT INTO quant.daily_trade_limits(symbol,trading_date,limit_up,limit_down,provider,available_at,raw)
           SELECT upper(row_data->>'ts_code'),to_date(row_data->>'trade_date','YYYYMMDD'),
                  nullif(row_data->>'up_limit','')::numeric,nullif(row_data->>'down_limit','')::numeric,%s,%s,row_data
             FROM stage
           ON CONFLICT(symbol,trading_date,provider) DO UPDATE SET
             limit_up=EXCLUDED.limit_up,limit_down=EXCLUDED.limit_down,
             available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
        (provider_key, available_at),
    )
    for table in ("market_bars_daily", "canonical_bars_daily"):
        connection.execute(
            f"""WITH stage AS (
                   SELECT DISTINCT ON(upper(row_data->>'ts_code'),row_data->>'trade_date') row_data
                     FROM annual_daily_stage WHERE row_data->>'trade_date' ~ '^\\d{{8}}$'
                    ORDER BY upper(row_data->>'ts_code'),row_data->>'trade_date',record_index DESC
               ) UPDATE quant.{table} bar
                   SET limit_up=nullif(stage.row_data->>'up_limit','')::numeric,
                       limit_down=nullif(stage.row_data->>'down_limit','')::numeric
                  FROM stage
                 WHERE bar.symbol=upper(stage.row_data->>'ts_code')
                   AND bar.trading_date=to_date(stage.row_data->>'trade_date','YYYYMMDD')"""
        )


def _persist_suspend_d(connection: Any, provider_key: str, available_at: datetime, _ingested_at: datetime,
                       _availability_basis: str) -> None:
    _persist_instruments_from_stage(connection, provider_key)
    connection.execute(
        """INSERT INTO quant.security_suspensions(
               symbol,suspend_date,resume_date,suspend_reason,provider,available_at,raw)
           SELECT upper(stage.row_data->>'ts_code'),to_date(stage.row_data->>'trade_date','YYYYMMDD'),
                  CASE WHEN stage.row_data->>'resume_date' ~ '^\\d{8}$'
                       THEN to_date(stage.row_data->>'resume_date','YYYYMMDD') END,
                  coalesce(stage.row_data->>'suspend_timing',stage.row_data->>'suspend_reason'),%s,%s,stage.row_data
             FROM (
               SELECT DISTINCT ON(upper(row_data->>'ts_code'),row_data->>'trade_date') row_data
                 FROM annual_daily_stage WHERE row_data->>'trade_date' ~ '^\\d{8}$'
                ORDER BY upper(row_data->>'ts_code'),row_data->>'trade_date',record_index DESC
             ) stage
           ON CONFLICT(symbol,suspend_date,provider) DO UPDATE SET
             resume_date=EXCLUDED.resume_date,suspend_reason=EXCLUDED.suspend_reason,
             available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
        (provider_key, available_at),
    )
    connection.execute(
        """WITH stage AS (
               SELECT DISTINCT ON(upper(row_data->>'ts_code'),row_data->>'trade_date') row_data
                 FROM annual_daily_stage WHERE row_data->>'trade_date' ~ '^\\d{8}$'
                ORDER BY upper(row_data->>'ts_code'),row_data->>'trade_date',record_index DESC
           ) UPDATE quant.canonical_bars_daily bar SET is_suspended=true,canonicalized_at=now()
              FROM stage
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
    connection: Any, provider_key: str, available_at: datetime, _ingested_at: datetime | None = None,
    _availability_basis: str | None = None, *, kind: str,
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
        """WITH stage AS (
               SELECT DISTINCT ON(row_data->>'ts_code') row_data
                 FROM annual_daily_stage
                WHERE row_data->>'ts_code' LIKE '%%.TI'
                ORDER BY row_data->>'ts_code',record_index DESC
           ) INSERT INTO quant.sectors(taxonomy_key,sector_key,label,metadata)
            SELECT %s,row_data->>'ts_code',row_data->>%s::text,
                   jsonb_build_object(%s::text,row_data->>%s::text)
              FROM stage
             WHERE nullif(row_data->>%s::text,'') IS NOT NULL
            ON CONFLICT(taxonomy_key,sector_key) DO UPDATE SET
              label=EXCLUDED.label,metadata=EXCLUDED.metadata,updated_at=now()""",
        (taxonomy_key, name_field, name_field, name_field, name_field),
    )
    if kind == "limit_strength":
        connection.execute(
            """WITH stage AS (
                   SELECT DISTINCT ON(row_data->>'ts_code',row_data->>'trade_date') row_data
                     FROM annual_daily_stage
                    WHERE row_data->>'trade_date' ~ '^\\d{8}$' AND row_data->>'ts_code' LIKE '%%.TI'
                    ORDER BY row_data->>'ts_code',row_data->>'trade_date',record_index DESC
               ) INSERT INTO quant.sector_market_observations(
                   taxonomy_key,sector_key,trading_date,provider_key,available_at,change_pct,constituent_count,raw)
               SELECT %s,row_data->>'ts_code',to_date(row_data->>'trade_date','YYYYMMDD'),%s,%s,
                      nullif(row_data->>'pct_chg','')::numeric,
                      nullif(row_data->>'cons_nums','')::integer,row_data
                 FROM stage
               ON CONFLICT(taxonomy_key,sector_key,trading_date,provider_key) DO UPDATE SET
                 available_at=EXCLUDED.available_at,change_pct=EXCLUDED.change_pct,
                 constituent_count=EXCLUDED.constituent_count,raw=EXCLUDED.raw""",
            (taxonomy_key, provider_key, available_at),
        )
        return
    connection.execute(
        """WITH stage AS (
               SELECT DISTINCT ON(row_data->>'ts_code',row_data->>'trade_date') row_data
                 FROM annual_daily_stage
                WHERE row_data->>'trade_date' ~ '^\\d{8}$' AND row_data->>'ts_code' LIKE '%%.TI'
                ORDER BY row_data->>'ts_code',row_data->>'trade_date',record_index DESC
           ) INSERT INTO quant.sector_market_observations(
               taxonomy_key,sector_key,trading_date,provider_key,available_at,close,change_pct,
               net_amount,net_buy_amount,net_sell_amount,constituent_count,leading_label,raw)
           SELECT %s,row_data->>'ts_code',to_date(row_data->>'trade_date','YYYYMMDD'),%s,%s,
                  nullif(row_data->>%s::text,'')::numeric,nullif(row_data->>'pct_change','')::numeric,
                  nullif(row_data->>'net_amount','')::numeric,nullif(row_data->>'net_buy_amount','')::numeric,
                  nullif(row_data->>'net_sell_amount','')::numeric,
                  nullif(row_data->>'company_num','')::integer,row_data->>'lead_stock',row_data
             FROM stage
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
    def __init__(
        self, database: Database, start_date: date, end_date: date, *, include_sector_events: bool = True,
        include_index: bool = True,
    ) -> None:
        validate_range(start_date, end_date)
        self.db = database
        self.start_date = start_date
        self.end_date = end_date
        self.include_sector_events = bool(include_sector_events)
        self.include_index = bool(include_index)
        self.providers = provider_configs()
        self.failures: list[dict[str, Any]] = []
        self.counts: dict[str, int] = {}
        # A backfill is a bounded batch, not a live retry loop.  Once a
        # provider/API route has failed, retain that first durable failure but
        # let the audited fallback serve its remaining dates.  This prevents a
        # dead proxy from adding a timeout per day while preserving the normal
        # live preference for Super GET in a fresh batch.
        self._batch_suppressed_candidates: dict[tuple[str, str], str] = {}

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

    def _completed_equivalent_exists(self, api_name: str, day: date | None) -> bool:
        """Do not refetch a completed date just to prefer a newer provider."""
        if day is None:
            return False
        with self.db.transaction() as connection:
            row = connection.execute(
                """SELECT 1 FROM quant.fetch_runs
                     WHERE capability=%s AND trade_date=%s AND status='completed'
                     LIMIT 1""",
                (f"annual:{api_name}", day),
            ).fetchone()
        return bool(row)

    def _completed_reference_equivalent_exists(self, api_name: str, params: dict[str, Any]) -> bool:
        """Reuse a completed range-level reference request from any provider.

        Unlike daily cross-sections, these tables have no per-day checkpoint.
        Their exact parameter object includes the date range or list status, so
        a completed physical source is sufficient evidence for this bounded
        bootstrap request and avoids needlessly retriggering a known-bad proxy.
        """
        with self.db.transaction() as connection:
            row = connection.execute(
                """SELECT 1 FROM quant.fetch_runs
                     WHERE capability=%s AND status='completed'
                       AND metadata @> %s::jsonb
                     LIMIT 1""",
                (f"annual:{api_name}", Json({"params": params})),
            ).fetchone()
        return bool(row)

    @staticmethod
    def _promote_staged(
        connection: Any, provider_key: str, promote: str, available_at: datetime,
        ingested_at: datetime, availability_basis: str,
    ) -> None:
        """Materialize derived facts from the caller's staged rows only."""
        if promote == "index_daily":
            _persist_daily(connection, provider_key, available_at, ingested_at, availability_basis, index_mode=True)
        elif promote in PROMOTERS:
            PROMOTERS[promote](connection, provider_key, available_at, ingested_at, availability_basis)
        elif promote in {"industry_flow", "concept_flow", "limit_strength"}:
            _persist_sector_flow(connection, provider_key, available_at, ingested_at, availability_basis, kind=promote)

    def _store(
        self, provider_key: str, api_name: str, key: str, rows: list[dict[str, Any]], promote: str,
        *, trade_date: date | None = None,
    ) -> None:
        ingested_at = datetime.now(timezone.utc)
        available_at = historical_daily_strategy_available_at(trade_date) if trade_date else ingested_at
        availability_basis = HISTORICAL_DAILY_AVAILABILITY_BASIS if trade_date else INGESTED_AVAILABILITY_BASIS
        with self.db.transaction() as connection:
            _stage_rows(connection, rows)
            _persist_raw(connection, provider_key, api_name, key, available_at, ingested_at, availability_basis)
            self._promote_staged(connection, provider_key, promote, available_at, ingested_at, availability_basis)

    async def fetch_one(
        self, spec: ApiSpec, params: dict[str, Any], *, day: date | None = None,
        row_filter: bool = True,
    ) -> str:
        if self._completed_equivalent_exists(spec.api_name, day):
            return "skipped"
        candidate_errors: list[dict[str, Any]] = []
        for provider_name in spec.provider_names:
            provider = self.providers[provider_name]
            suppressed = self._batch_suppressed_candidates.get((spec.api_name, provider.key))
            # Sanitisation can intentionally erase a provider error's text;
            # an empty string still means the route failed and must not be
            # retried for every later date in this bounded batch.
            if suppressed is not None:
                candidate_errors.append({
                    "provider": provider.key,
                    "error": f"batch-local failover active: {suppressed or 'detail redacted'}",
                })
                continue
            key, skip = self._prepare_run(provider.key, spec.api_name, params, day)
            if skip:
                return "skipped"
            try:
                filtered: list[dict[str, Any]] = []
                # A coverage threshold makes transient empty cross-sections
                # distinguishable from legal-empty event APIs. Retry the same
                # physical source first; only then use the declared fallback.
                attempts = 3 if spec.minimum_rows else 1
                for attempt in range(attempts):
                    rows = await call_provider(provider, spec.api_name, params, None)
                    filtered = valid_rows(spec.api_name, rows, day) if row_filter else [dict(row) for row in rows]
                    if len(filtered) >= spec.minimum_rows:
                        break
                    if attempt + 1 < attempts:
                        await asyncio.sleep(float(2 ** attempt))
                if len(filtered) < spec.minimum_rows:
                    raise ProviderCallError(
                        f"{spec.api_name} returned {len(filtered)} valid rows; expected at least {spec.minimum_rows}"
                    )
                if not filtered and not spec.legal_empty and spec.minimum_rows == 0:
                    raise ProviderCallError(f"{spec.api_name} returned an unexpected empty response")
                self._store(provider.key, spec.api_name, key, filtered, spec.promote, trade_date=day)
                self._finish_run(key, len(filtered))
                self.counts[spec.api_name] = self.counts.get(spec.api_name, 0) + len(filtered)
                return "completed"
            except Exception as error:  # noqa: BLE001 - durable failure ledger is intentional
                self._fail_run(key, error)
                self._batch_suppressed_candidates[(spec.api_name, provider.key)] = safe_error_detail(
                    str(error), 180,
                )
                candidate_errors.append({
                    "provider": provider.key, "error": safe_error_detail(str(error), 300),
                })
        self.failures.append({
            "api_name": spec.api_name, "trade_date": str(day) if day else None,
            "provider_attempts": candidate_errors,
        })
        return "failed"

    async def _bootstrap_reference(
        self,
        api_name: str,
        params: dict[str, Any],
        *,
        provider_names: tuple[str, ...],
        minimum_rows: int,
        normalize_rows: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
        persist: Callable[[Any, str, datetime], None],
    ) -> None:
        """Fetch a range-level control table through audited provider fallbacks.

        Range-level reference tables have no per-trading-day checkpoint, so the
        provider fallback must be explicit here as well as in ``fetch_one``.
        A failed legacy route is retained in ``fetch_runs`` for audit, but may
        not prevent a verified provider from completing the same request.
        """
        candidate_errors: list[dict[str, str]] = []
        if self._completed_reference_equivalent_exists(api_name, params):
            return
        for provider_name in provider_names:
            provider = self.providers[provider_name]
            key, skip = self._prepare_run(provider.key, api_name, params, None)
            if skip:
                return
            try:
                rows = normalize_rows(await call_provider(provider, api_name, params, None))
                if len(rows) < minimum_rows:
                    raise ProviderCallError(
                        f"{api_name} returned only {len(rows)} rows; expected at least {minimum_rows}"
                    )
                available_at = datetime.now(timezone.utc)
                with self.db.transaction() as connection:
                    _stage_rows(connection, rows)
                    _persist_raw(connection, provider.key, api_name, key, available_at, available_at,
                                 INGESTED_AVAILABILITY_BASIS)
                    persist(connection, provider.key, available_at)
                self._finish_run(key, len(rows))
                return
            except Exception as error:  # noqa: BLE001 - preserve each provider failure durably
                self._fail_run(key, error)
                candidate_errors.append({
                    "provider": provider.key,
                    "error": safe_error_detail(str(error), 300),
                })
        raise ProviderCallError(f"{api_name} failed through every declared provider: {candidate_errors}")

    async def bootstrap(self) -> list[date]:
        calendar_params = {
            "exchange": "SSE", "start_date": self.start_date.strftime("%Y%m%d"),
            "end_date": self.end_date.strftime("%Y%m%d"),
        }
        expected_calendar_rows = (self.end_date - self.start_date).days + 1
        await self._bootstrap_reference(
            "trade_cal", calendar_params,
            provider_names=("super_sdk", "primary"),
            minimum_rows=expected_calendar_rows,
            normalize_rows=lambda rows: [dict(row) for row in rows],
            persist=_persist_trade_calendar,
        )

        for status in ("L", "D", "P"):
            params = {"exchange": "", "list_status": status}
            await self._bootstrap_reference(
                "stock_basic", params,
                provider_names=("super_sdk", "primary"),
                minimum_rows=4_800 if status == "L" else 0,
                normalize_rows=lambda rows, status=status: [
                    dict(row, _list_status=status)
                    for row in rows
                    if STOCK_CODE.fullmatch(str(row.get("ts_code") or "").upper())
                ],
                persist=_persist_stock_basic,
            )

        with self.db.transaction() as connection:
            rows = connection.execute(
                """SELECT calendar_date FROM quant.market_trade_calendar
                    WHERE exchange='SSE' AND is_open AND calendar_date BETWEEN %s AND %s
                    ORDER BY calendar_date""",
                (self.start_date, self.end_date),
            ).fetchall()
        days = [row["calendar_date"] for row in rows]
        # ``trade_cal`` above has already verified every calendar date in the
        # requested range.  A focused repair window legitimately contains far
        # fewer than a year's roughly 240 open days; require only that it is
        # not an all-holiday/malformed response.
        if not days:
            raise RuntimeError("calendar produced no open days for the requested range")
        return days

    async def core_lane(self, days: list[date]) -> None:
        for index, day in enumerate(days, start=1):
            stamp = day.strftime("%Y%m%d")
            # The primary contract is audited at 60 requests/minute.  Five
            # independent cross-sections can be fetched concurrently; the
            # shared limiter still enforces the supplier window, while final
            # control reconciliation removes any canonical write-order race.
            await asyncio.gather(*(
                self.fetch_one(spec, {"trade_date": stamp}, day=day)
                for spec in CORE_DAILY_SPECS
            ))
            if index == 1 or index % 10 == 0 or index == len(days):
                print(json.dumps({"lane": "core", "day": str(day), "progress": f"{index}/{len(days)}", "failures": len(self.failures)}), flush=True)

    async def sector_lane(self, days: list[date]) -> None:
        for index, day in enumerate(days, start=1):
            stamp = day.strftime("%Y%m%d")
            # The City/SDK route is slower and audited at 30 requests/minute.
            # Keep concurrency to three; the provider limiter provides the
            # rolling-window backpressure across batches and dates.
            for offset in range(0, len(SECTOR_EVENT_SPECS), 3):
                await asyncio.gather(*(
                    self.fetch_one(spec, {"trade_date": stamp}, day=day)
                    for spec in SECTOR_EVENT_SPECS[offset:offset + 3]
                ))
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
                # The response spans a year. Project one daily availability
                # clock per bar instead of stamping every past index close with
                # the range request's final import time.
                by_day: dict[date, list[dict[str, Any]]] = {}
                for row in rows:
                    stamp = str(row.get("trade_date") or "")
                    try:
                        row_day = datetime.strptime(stamp, "%Y%m%d").date()
                    except ValueError:
                        continue
                    by_day.setdefault(row_day, []).append(dict(row))
                if not by_day:
                    raise ProviderCallError(f"index_daily {symbol} returned no dated rows")
                for row_day, day_rows in by_day.items():
                    self._store(provider.key, "index_daily", key, day_rows, "index_daily", trade_date=row_day)
                self._finish_run(key, len(rows))
                self.counts["index_daily"] = self.counts.get("index_daily", 0) + len(rows)
            except Exception as error:
                self._fail_run(key, error)
                self.failures.append({"api_name": "index_daily", "symbol": symbol, "error": safe_error_detail(str(error), 300)})

    def reconcile_suspensions(self) -> None:
        """Rejoin daily controls after concurrent source materialization."""
        with self.db.transaction() as connection:
            for table in ("market_bars_daily", "canonical_bars_daily"):
                connection.execute(
                    f"""UPDATE quant.{table} bar SET adj_factor=factor.adj_factor
                          FROM (
                              SELECT DISTINCT ON(symbol,trading_date)
                                     symbol,trading_date,adj_factor
                                FROM quant.daily_adjustment_factors
                               WHERE trading_date BETWEEN %s AND %s
                               ORDER BY symbol,trading_date,
                                        CASE provider
                                          WHEN 'tushare_super_sdk' THEN 0
                                          WHEN 'tushare_super_get' THEN 1
                                          WHEN 'tushare_primary' THEN 2
                                          ELSE 9 END,
                                        available_at DESC
                          ) factor
                         WHERE factor.trading_date BETWEEN %s AND %s
                           AND bar.trading_date BETWEEN %s AND %s
                           AND bar.symbol=factor.symbol AND bar.trading_date=factor.trading_date""",
                    (self.start_date, self.end_date, self.start_date, self.end_date,
                     self.start_date, self.end_date),
                )
                connection.execute(
                    f"""UPDATE quant.{table} bar
                           SET limit_up=limits.limit_up,limit_down=limits.limit_down
                          FROM (
                              SELECT DISTINCT ON(symbol,trading_date)
                                     symbol,trading_date,limit_up,limit_down
                                FROM quant.daily_trade_limits
                               WHERE trading_date BETWEEN %s AND %s
                               ORDER BY symbol,trading_date,
                                        CASE provider
                                          WHEN 'tushare_super_sdk' THEN 0
                                          WHEN 'tushare_super_get' THEN 1
                                          WHEN 'tushare_primary' THEN 2
                                          ELSE 9 END,
                                        available_at DESC
                          ) limits
                         WHERE limits.trading_date BETWEEN %s AND %s
                           AND bar.trading_date BETWEEN %s AND %s
                           AND bar.symbol=limits.symbol AND bar.trading_date=limits.trading_date""",
                    (self.start_date, self.end_date, self.start_date, self.end_date,
                     self.start_date, self.end_date),
                )
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

    def promote_stored_sector_flows(self) -> dict[str, int]:
        """Repair/materialize sector rows from retained raw evidence only.

        Fetch checkpoints and canonical promotion are deliberately separate:
        a corrected promotion rule must be able to rebuild without spending a
        provider request or rewriting the immutable raw evidence.
        """
        mappings = (
            ("moneyflow_ind_ths", "industry_flow"),
            ("moneyflow_cnt_ths", "concept_flow"),
            ("limit_cpt_list", "limit_strength"),
        )
        counts: dict[str, int] = {}
        for api_name, kind in mappings:
            with self.db.transaction() as connection:
                raw_rows = connection.execute(
                    """SELECT DISTINCT ON(row_data->>'trade_date',row_data->>'ts_code') row_data,available_at
                         FROM quant.tushare_raw_records
                        WHERE provider_key='tushare_super_sdk' AND api_name=%s
                          AND row_data->>'ts_code' LIKE '%%.TI'
                          AND row_data->>'trade_date' ~ '^[0-9]{8}$'
                          AND to_date(row_data->>'trade_date','YYYYMMDD') BETWEEN %s AND %s
                        ORDER BY row_data->>'trade_date',row_data->>'ts_code',available_at DESC""",
                    (api_name, self.start_date, self.end_date),
                ).fetchall()
            grouped: dict[str, list[dict[str, Any]]] = {}
            available_by_date: dict[str, datetime] = {}
            for raw in raw_rows:
                row = dict(raw["row_data"])
                stamp = str(row.get("trade_date") or "")
                grouped.setdefault(stamp, []).append(row)
                available_by_date[stamp] = max(
                    available_by_date.get(stamp, raw["available_at"]), raw["available_at"],
                )
            for stamp, rows in grouped.items():
                with self.db.transaction() as connection:
                    _stage_rows(connection, rows)
                    _persist_sector_flow(
                        connection, "tushare_super_sdk", available_by_date[stamp], kind=kind,
                    )
            counts[api_name] = len(raw_rows)
        return counts

    def materialize_daily_market_aggregates(self) -> dict[str, Any]:
        """Build close-only breadth and volume using explicit Tushare units."""
        with self.db.transaction() as connection:
            connection.execute(
                """WITH stock_bars AS (
                       SELECT trading_date,close,pre_close,amount,volume,available_at,
                              CASE WHEN pre_close>0 THEN (close/pre_close-1)*100 END AS change_pct
                         FROM quant.canonical_bars_daily
                        WHERE trading_date BETWEEN %s AND %s
                          AND symbol ~ '^(?:(?:60[0135]|68[89])[0-9]{3}\\.SH|(?:000|001|002|003|300|301)[0-9]{3}\\.SZ|[489][0-9]{5}\\.BJ)$'
                     ), aggregate AS (
                       SELECT trading_date,count(*)::integer AS stock_count,
                              count(*) FILTER (WHERE change_pct>0)::integer AS advancers,
                              count(*) FILTER (WHERE change_pct<0)::integer AS decliners,
                              count(*) FILTER (WHERE change_pct=0)::integer AS unchanged,
                              percentile_cont(0.5) WITHIN GROUP (ORDER BY change_pct)
                                FILTER (WHERE change_pct IS NOT NULL) AS median_change_pct,
                              avg(change_pct) FILTER (WHERE change_pct IS NOT NULL) AS mean_change_pct,
                              sum(amount) AS total_amount_kcny,sum(volume) AS total_volume_lots,
                              max(available_at) AS available_at
                         FROM stock_bars GROUP BY trading_date
                     )
                   INSERT INTO quant.daily_market_aggregates(
                       trading_date,stock_count,advancers,decliners,unchanged,median_change_pct,
                       mean_change_pct,total_amount_kcny,total_volume_lots,source_provider,available_at,quality_flags)
                   SELECT trading_date,stock_count,advancers,decliners,unchanged,median_change_pct,
                          mean_change_pct,total_amount_kcny,total_volume_lots,'canonical_daily_multi_provider',available_at,
                          CASE WHEN stock_count<4800 THEN '["stock_coverage_below_4800"]'::jsonb ELSE '[]'::jsonb END
                     FROM aggregate
                   ON CONFLICT(trading_date) DO UPDATE SET
                     stock_count=EXCLUDED.stock_count,advancers=EXCLUDED.advancers,
                     decliners=EXCLUDED.decliners,unchanged=EXCLUDED.unchanged,
                     median_change_pct=EXCLUDED.median_change_pct,mean_change_pct=EXCLUDED.mean_change_pct,
                     total_amount_kcny=EXCLUDED.total_amount_kcny,total_volume_lots=EXCLUDED.total_volume_lots,
                     source_provider=EXCLUDED.source_provider,available_at=EXCLUDED.available_at,
                     quality_flags=EXCLUDED.quality_flags,updated_at=now()""",
                (self.start_date, self.end_date),
            )
            row = connection.execute(
                """SELECT count(*)::int rows,min(trading_date) start_date,max(trading_date) end_date,
                          count(*) FILTER (WHERE quality_flags='[]'::jsonb)::int complete_days
                     FROM quant.daily_market_aggregates WHERE trading_date BETWEEN %s AND %s""",
                (self.start_date, self.end_date),
            ).fetchone()
        return dict(row)

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

    def reproject_stored_historical_clocks(self) -> tuple[list[date], dict[str, int]]:
        """Promote retained daily raw rows with the declared PIT clock only.

        This repairs the one-year baseline created before the dual-clock
        contract without calling a provider or changing raw row content. The
        original receipt timestamp remains recoverable as ``ingested_at`` on
        the facts being reprojected.
        """
        specs = (*CORE_DAILY_SPECS, *SECTOR_EVENT_SPECS, ApiSpec("index_daily", "primary", promote="index_daily"))
        api_names = [item.api_name for item in specs]
        with self.db.transaction() as connection:
            day_rows = connection.execute(
                """SELECT DISTINCT to_date(row_data->>'trade_date','YYYYMMDD') AS trading_date
                     FROM quant.tushare_raw_records
                    WHERE api_name=ANY(%s) AND row_data->>'trade_date' ~ '^\\d{8}$'
                      AND to_date(row_data->>'trade_date','YYYYMMDD') BETWEEN %s AND %s
                    ORDER BY trading_date""",
                (api_names, self.start_date, self.end_date),
            ).fetchall()
        days = [row["trading_date"] for row in day_rows]
        counts: dict[str, int] = {}
        for day in days:
            stamp = day.strftime("%Y%m%d")
            available_at = historical_daily_strategy_available_at(day)
            for spec in specs:
                provider_key = self.providers[spec.provider_name].key
                with self.db.transaction() as connection:
                    rows = connection.execute(
                        """SELECT row_data FROM quant.tushare_raw_records
                             WHERE provider_key=%s AND api_name=%s AND row_data->>'trade_date'=%s
                             ORDER BY record_index""",
                        (provider_key, spec.api_name, stamp),
                    ).fetchall()
                payload = valid_rows(spec.api_name, [dict(row["row_data"]) for row in rows], day)
                if not payload:
                    continue
                # This is a local correction of facts that already exist.  Do
                # not route through ``_persist_raw``: legacy receipts use a
                # different stable record-key convention, and doing so would
                # create duplicate raw rows even though the content is known.
                # Preserve the old availability value as the actual local
                # receipt clock before assigning the declared strategy clock.
                with self.db.transaction() as connection:
                    connection.execute(
                        """UPDATE quant.tushare_raw_records
                              SET ingested_at=coalesce(ingested_at,available_at),
                                  available_at=least(available_at,%s),
                                  availability_basis=%s
                            WHERE provider_key=%s AND api_name=%s AND row_data->>'trade_date'=%s""",
                        (available_at, HISTORICAL_DAILY_AVAILABILITY_BASIS, provider_key, spec.api_name, stamp),
                    )
                    _stage_rows(connection, payload)
                    self._promote_staged(
                        connection, provider_key, spec.promote, available_at,
                        datetime.now(timezone.utc), HISTORICAL_DAILY_AVAILABILITY_BASIS,
                    )
                counts[spec.api_name] = counts.get(spec.api_name, 0) + len(payload)
        return days, counts

    async def run(self, *, reproject_only: bool = False) -> dict[str, Any]:
        if reproject_only:
            days, reprojection_counts = self.reproject_stored_historical_clocks()
            if not days:
                raise RuntimeError("no stored dated annual raw rows exist in the requested range")
            print(json.dumps({
                "status": "reprojecting", "start_date": str(self.start_date), "end_date": str(self.end_date),
                "open_days": len(days), "provider_requests": 0,
            }), flush=True)
        else:
            days = await self.bootstrap()
            reprojection_counts = {}
        print(json.dumps({
            "status": "started", "start_date": str(self.start_date), "end_date": str(self.end_date),
            "open_days": len(days), "minute_data": False, "reproject_only": reproject_only,
        }), flush=True)
        if not reproject_only:
            lanes = [self.core_lane(days)]
            if self.include_index:
                lanes.append(self.index_lane())
            if self.include_sector_events:
                lanes.append(self.sector_lane(days))
            await asyncio.gather(*lanes)
        self.reconcile_suspensions()
        sector_promotions = (
            self.promote_stored_sector_flows()
            if self.include_sector_events else {"status": "explicitly_skipped_for_this_range"}
        )
        market_aggregates = self.materialize_daily_market_aggregates()
        with self.db.transaction() as connection:
            universe_membership = rebuild_historical_membership_from_canonical(connection, "all_a")
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
            "reproject_only": reproject_only, "reprojected_row_counts": reprojection_counts,
            "sector_events": "included" if self.include_sector_events else "explicitly_skipped_for_this_range",
            "index_daily": "included" if self.include_index else "explicitly_skipped_for_this_range",
            "coverage": dict(coverage), "market_aggregates": market_aggregates,
            "universe_membership": universe_membership,
            "sector_promotions": sector_promotions,
            "sector_features": feature_result,
            "failure_count": len(self.failures), "failures": self.failures[:50],
        }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Backfill one bounded year of non-minute China market data")
    command.add_argument("--start-date", required=True, type=parse_iso_date)
    command.add_argument("--end-date", required=True, type=parse_iso_date)
    command.add_argument(
        "--reproject-only", action="store_true",
        help="use already retained rows to repair strategy availability clocks; makes no provider request",
    )
    command.add_argument(
        "--confirm-historical-backfill", default=None,
        help="required explicit acknowledgement before any historical provider request",
    )
    command.add_argument(
        "--skip-sector-events", action="store_true",
        help=("backfill only P2 daily/control-plane data for this range; records the sector-flow history gap "
              "instead of repeatedly treating an unavailable historical upstream response as complete"),
    )
    command.add_argument(
        "--skip-index", action="store_true",
        help="skip index_daily for a targeted daily/control-plane repair without altering existing index evidence",
    )
    return command


async def async_main() -> int:
    command = parser()
    args = command.parse_args()
    if not args.reproject_only:
        try:
            validate_historical_backfill_confirmation(args.confirm_historical_backfill)
        except ValueError as error:
            command.error(str(error))
    database = Database()
    try:
        result = await AnnualDailyBackfill(
            database, args.start_date, args.end_date, include_sector_events=not args.skip_sector_events,
            include_index=not args.skip_index,
        ).run(
            reproject_only=args.reproject_only,
        )
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
    "HISTORICAL_BACKFILL_CONFIRMATION", "INDEX_CODES", "request_key", "valid_rows",
    "validate_historical_backfill_confirmation", "validate_range",
]
