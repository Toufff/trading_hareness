"""Set-based batch upsert for canonical daily bars.

``daily_bar_repository.upsert_daily_bar`` runs 5-6 statements per bar,
including a per-bar ``SELECT ... FOR UPDATE``-less read-then-write against
``quant.canonical_bars_daily``.  Called once per symbol for a full A-share
cross-section (~5,500 symbols), that is ~30,000 statements in a single
transaction.  ``upsert_daily_bars`` here batches the same write contract
(provider-priority selection, immutable raw evidence, amount-unit
quarantine, ``tencent_free`` rejection) into a small, fixed number of
set-based statements regardless of batch size.

Semantics are intentionally identical to ``upsert_daily_bar`` for the common
case of at most one bar per ``(symbol, trading_date)`` in a single call
(true for every real caller: one provider response per sync).  If the input
contains more than one bar for the same ``(symbol, trading_date)`` (which no
current caller does), only the last one determines the final
``market_bars_daily``/``canonical_bars_daily`` row -- matching what a
sequential loop of ``upsert_daily_bar`` calls would leave behind -- but the
provider-priority/close-conflict comparison is evaluated against the
pre-batch database state for all of them instead of incrementally.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

from .analysis import as_utc
from .daily_bar_repository import (
    TUSHARE_DAILY_AMOUNT_RATIO_MAX,
    TUSHARE_DAILY_AMOUNT_RATIO_MIN,
    daily_amount_unit_mismatch,
    exchange_for,
    provider_priority,
)
from .request_models import DailyBar


def _decimal_str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def upsert_daily_bars(connection: Any, bars: Sequence[DailyBar]) -> int:
    """Batch-persist licensed/unadjusted daily bars; see module docstring."""
    if not bars:
        return 0
    for bar in bars:
        if bar.source == "tencent_free":
            raise ValueError("tencent_free front-adjusted daily rows are raw research evidence only")

    symbols = sorted({bar.symbol for bar in bars})

    existing_instruments = {
        row["symbol"]: row
        for row in connection.execute(
            "SELECT symbol,name,industry,is_st FROM quant.instruments WHERE symbol=ANY(%s)",
            (symbols,),
        ).fetchall()
    }
    dates = [bar.trading_date for bar in bars]
    existing_canonical = {
        (row["symbol"], row["trading_date"]): row
        for row in connection.execute(
            """SELECT c.symbol,c.trading_date,c.close,c.selected_provider,c.source_observation_ids,
                      c.adj_factor,c.is_suspended,c.limit_up,c.limit_down
                 FROM quant.canonical_bars_daily c
                 JOIN unnest(%s::text[],%s::date[]) AS k(symbol,trading_date)
                   ON c.symbol=k.symbol AND c.trading_date=k.trading_date""",
            ([bar.symbol for bar in bars], dates),
        ).fetchall()
    }
    existing_market = {
        (row["symbol"], row["trading_date"]): row
        for row in connection.execute(
            """SELECT m.symbol,m.trading_date,m.adj_factor,m.is_suspended,m.limit_up,m.limit_down
                 FROM quant.market_bars_daily m
                 JOIN unnest(%s::text[],%s::date[]) AS k(symbol,trading_date)
                   ON m.symbol=k.symbol AND m.trading_date=k.trading_date""",
            ([bar.symbol for bar in bars], dates),
        ).fetchall()
    }

    # --- instruments: last non-null value per symbol wins, else keep old ---
    instrument_entries: dict[str, dict[str, Any]] = {}
    for bar in bars:
        entry = instrument_entries.setdefault(bar.symbol, {"name": None, "industry": None, "is_st": None})
        if bar.name is not None:
            entry["name"] = bar.name
        if bar.industry is not None:
            entry["industry"] = bar.industry
        if bar.is_st is not None:
            entry["is_st"] = bar.is_st
        entry["source"] = bar.source
    inst_symbols, inst_exchanges, inst_names, inst_industries, inst_is_st, inst_sources = [], [], [], [], [], []
    for symbol, entry in instrument_entries.items():
        old = existing_instruments.get(symbol)
        inst_symbols.append(symbol)
        inst_exchanges.append(exchange_for(symbol))
        inst_names.append(entry["name"] if entry["name"] is not None else (old["name"] if old else None))
        inst_industries.append(entry["industry"] if entry["industry"] is not None else (old["industry"] if old else None))
        inst_is_st.append(entry["is_st"] if entry["is_st"] is not None else (old["is_st"] if old else False))
        inst_sources.append(entry["source"])
    connection.execute(
        """INSERT INTO quant.instruments(symbol,exchange,name,industry,is_st,source)
           SELECT * FROM unnest(%s::text[],%s::text[],%s::text[],%s::text[],%s::boolean[],%s::text[])
           ON CONFLICT(symbol) DO UPDATE SET exchange=EXCLUDED.exchange,name=EXCLUDED.name,
             industry=EXCLUDED.industry,is_st=EXCLUDED.is_st,source=EXCLUDED.source,updated_at=now()""",
        (inst_symbols, inst_exchanges, inst_names, inst_industries, inst_is_st, inst_sources),
    )

    # --- per-bar computed fields shared by market_bars_daily/canonical/raw evidence ---
    amount_mismatch: list[bool] = []
    promoted_amount: list[Decimal | None] = []
    available_at_utc: list[datetime] = []
    for bar in bars:
        mismatch = daily_amount_unit_mismatch(source=bar.source, amount=bar.amount, volume=bar.volume, close=bar.close)
        amount_mismatch.append(mismatch)
        promoted_amount.append(None if mismatch else bar.amount)
        available_at_utc.append(as_utc(bar.available_at))

    # --- raw evidence: always one row per input bar, keyed by row_index for exact correspondence ---
    provider_arr, symbol_arr, effective_arr, avail_arr, sha_arr, normalized_arr, index_arr = [], [], [], [], [], [], []
    for index, bar in enumerate(bars):
        normalized = bar.model_dump(mode="json")
        payload_sha256 = hashlib.sha256(repr(sorted(normalized.items())).encode("utf-8")).hexdigest()
        provider_arr.append(bar.source)
        symbol_arr.append(bar.symbol)
        effective_arr.append(datetime.combine(bar.trading_date, datetime.min.time(), tzinfo=timezone.utc))
        avail_arr.append(available_at_utc[index])
        sha_arr.append(payload_sha256)
        normalized_arr.append(json.dumps(normalized, ensure_ascii=False, sort_keys=True))
        index_arr.append(index)
    observation_rows = connection.execute(
        """INSERT INTO quant.raw_market_observations(provider_key,capability,market,symbol,effective_at,available_at,payload_sha256,normalized,payload)
           SELECT t.provider_key,'daily_bar','cn',t.symbol,t.effective_at,t.available_at,t.payload_sha256,
                  t.normalized_json::jsonb,t.normalized_json::jsonb
             FROM unnest(%s::text[],%s::text[],%s::timestamptz[],%s::timestamptz[],%s::text[],%s::text[],%s::integer[])
                  AS t(provider_key,symbol,effective_at,available_at,payload_sha256,normalized_json,row_index)
           ON CONFLICT(provider_key,capability,market,symbol,effective_at,payload_sha256) DO UPDATE SET available_at=EXCLUDED.available_at
           RETURNING row_index,observation_id""",
        (provider_arr, symbol_arr, effective_arr, avail_arr, sha_arr, normalized_arr, index_arr),
    ).fetchall()
    # ``RETURNING`` on a plain projection preserves the scan order of a single
    # INSERT statement in every observed PostgreSQL version, but relying on
    # order is unnecessary: row_index round-trips through the query, so the
    # mapping below is correct even if that ever changed.
    observation_id_by_index = {row["row_index"]: row["observation_id"] for row in observation_rows}

    # --- market_bars_daily: last bar per (symbol, trading_date) wins ---
    mb_final: dict[tuple[str, Any], dict[str, Any]] = {}
    last_index_by_key: dict[tuple[str, Any], int] = {}
    for index, bar in enumerate(bars):
        key = (bar.symbol, bar.trading_date)
        old_mb = existing_market.get(key)
        mb_final[key] = {
            "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close, "pre_close": bar.pre_close,
            "volume": bar.volume, "amount": promoted_amount[index],
            "adj_factor": bar.adj_factor if bar.adj_factor is not None else (old_mb["adj_factor"] if old_mb else None),
            "is_suspended": bar.is_suspended if bar.is_suspended is not None else (old_mb["is_suspended"] if old_mb else False),
            "limit_up": bar.limit_up if bar.limit_up is not None else (old_mb["limit_up"] if old_mb else None),
            "limit_down": bar.limit_down if bar.limit_down is not None else (old_mb["limit_down"] if old_mb else None),
            "source": bar.source, "available_at": available_at_utc[index],
        }
        last_index_by_key[key] = index
    keys = list(mb_final.keys())
    connection.execute(
        """INSERT INTO quant.market_bars_daily(symbol,trading_date,open,high,low,close,pre_close,volume,amount,adj_factor,is_suspended,limit_up,limit_down,source,available_at)
           SELECT * FROM unnest(%s::text[],%s::date[],%s::numeric[],%s::numeric[],%s::numeric[],%s::numeric[],%s::numeric[],
                                 %s::numeric[],%s::numeric[],%s::numeric[],%s::boolean[],%s::numeric[],%s::numeric[],%s::text[],%s::timestamptz[])
           ON CONFLICT(symbol,trading_date) DO UPDATE SET open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
             close=EXCLUDED.close,pre_close=EXCLUDED.pre_close,volume=EXCLUDED.volume,amount=EXCLUDED.amount,
             adj_factor=EXCLUDED.adj_factor,is_suspended=EXCLUDED.is_suspended,limit_up=EXCLUDED.limit_up,
             limit_down=EXCLUDED.limit_down,source=EXCLUDED.source,available_at=EXCLUDED.available_at""",
        (
            [key[0] for key in keys], [key[1] for key in keys],
            [mb_final[key]["open"] for key in keys], [mb_final[key]["high"] for key in keys],
            [mb_final[key]["low"] for key in keys], [mb_final[key]["close"] for key in keys],
            [mb_final[key]["pre_close"] for key in keys], [mb_final[key]["volume"] for key in keys],
            [mb_final[key]["amount"] for key in keys], [mb_final[key]["adj_factor"] for key in keys],
            [mb_final[key]["is_suspended"] for key in keys], [mb_final[key]["limit_up"] for key in keys],
            [mb_final[key]["limit_down"] for key in keys], [mb_final[key]["source"] for key in keys],
            [mb_final[key]["available_at"] for key in keys],
        ),
    )

    # --- amount-unit quarantine issues (only for mismatched bars, deduplicated like the per-row helper) ---
    issue_symbols, issue_dates, issue_details = [], [], []
    for index, bar in enumerate(bars):
        if not amount_mismatch[index]:
            continue
        implied_ratio = bar.amount / (bar.volume * bar.close)
        issue_symbols.append(bar.symbol)
        issue_dates.append(bar.trading_date)
        issue_details.append(json.dumps({
            "provider": bar.source, "amount": _decimal_str(bar.amount), "volume_lot": _decimal_str(bar.volume),
            "close": _decimal_str(bar.close), "implied_amount_per_lot_close": _decimal_str(implied_ratio),
            "expected_ratio_range": [str(TUSHARE_DAILY_AMOUNT_RATIO_MIN), str(TUSHARE_DAILY_AMOUNT_RATIO_MAX)],
            "action": "amount_quarantined_not_rescaled",
        }))
    if issue_symbols:
        connection.execute(
            """INSERT INTO quant.data_quality_issues(capability,symbol,trading_date,severity,code,message,details)
               SELECT 'daily_bar',t.symbol,t.trading_date,'warning','daily_amount_unit_mismatch',
                      'daily amount does not match the Tushare lots/thousand-yuan contract',t.details::jsonb
                 FROM unnest(%s::text[],%s::date[],%s::text[]) AS t(symbol,trading_date,details)
                WHERE NOT EXISTS (
                    SELECT 1 FROM quant.data_quality_issues
                     WHERE capability='daily_bar' AND symbol=t.symbol AND trading_date=t.trading_date
                       AND code='daily_amount_unit_mismatch' AND resolved_at IS NULL
                )""",
            (issue_symbols, issue_dates, issue_details),
        )

    # --- provider close-conflict issues (only for bars whose canonical close disagrees) ---
    conflict_symbols, conflict_dates, conflict_details = [], [], []
    for bar in bars:
        existing = existing_canonical.get((bar.symbol, bar.trading_date))
        if existing and existing["close"] and abs(Decimal(existing["close"]) - bar.close) > Decimal("0.001"):
            conflict_symbols.append(bar.symbol)
            conflict_dates.append(bar.trading_date)
            conflict_details.append(json.dumps({
                "existing_provider": existing["selected_provider"], "existing_close": str(existing["close"]),
                "incoming_provider": bar.source, "incoming_close": str(bar.close),
            }))
    if conflict_symbols:
        connection.execute(
            """INSERT INTO quant.data_quality_issues(capability,symbol,trading_date,severity,code,message,details)
               SELECT 'daily_bar',t.symbol,t.trading_date,'warning','provider_close_conflict',
                      'daily close differs across providers',t.details::jsonb
                 FROM unnest(%s::text[],%s::date[],%s::text[]) AS t(symbol,trading_date,details)""",
            (conflict_symbols, conflict_dates, conflict_details),
        )

    # --- canonical_bars_daily: provider-priority replace decision, last bar per key wins ---
    replace_keys: list[tuple[str, Any]] = []
    update_only_keys: list[tuple[str, Any]] = []
    canonical_final: dict[tuple[str, Any], dict[str, Any]] = {}
    for key, index in last_index_by_key.items():
        bar = bars[index]
        existing = existing_canonical.get(key)
        merged_source_ids = ([str(value) for value in (existing["source_observation_ids"] or [])] if existing else [])
        merged_source_ids.append(str(observation_id_by_index[index]))
        replace = existing is None or provider_priority(bar.source) <= provider_priority(str(existing["selected_provider"]))
        canonical_final[key] = {
            "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close, "pre_close": bar.pre_close,
            "volume": bar.volume, "amount": promoted_amount[index],
            "adj_factor": bar.adj_factor if bar.adj_factor is not None else (existing["adj_factor"] if existing else None),
            "is_suspended": bar.is_suspended if bar.is_suspended is not None else (existing["is_suspended"] if existing else False),
            "limit_up": bar.limit_up if bar.limit_up is not None else (existing["limit_up"] if existing else None),
            "limit_down": bar.limit_down if bar.limit_down is not None else (existing["limit_down"] if existing else None),
            "selected_provider": bar.source if replace else str(existing["selected_provider"]),
            "source_ids_json": json.dumps(merged_source_ids),
            "quality_status": "partial" if amount_mismatch[index] else "fresh",
            "available_at": available_at_utc[index],
        }
        (replace_keys if replace else update_only_keys).append(key)

    if replace_keys:
        rows = [canonical_final[key] for key in replace_keys]
        connection.execute(
            """INSERT INTO quant.canonical_bars_daily(symbol,trading_date,open,high,low,close,pre_close,volume,amount,
                   adj_factor,is_suspended,limit_up,limit_down,selected_provider,source_observation_ids,quality_status,available_at)
               SELECT t.symbol,t.trading_date,t.open,t.high,t.low,t.close,t.pre_close,t.volume,t.amount,
                      t.adj_factor,t.is_suspended,t.limit_up,t.limit_down,t.selected_provider,
                      (SELECT array_agg(elem::uuid) FROM jsonb_array_elements_text(t.source_ids_json::jsonb) elem),
                      t.quality_status,t.available_at
                 FROM unnest(%s::text[],%s::date[],%s::numeric[],%s::numeric[],%s::numeric[],%s::numeric[],%s::numeric[],
                              %s::numeric[],%s::numeric[],%s::numeric[],%s::boolean[],%s::numeric[],%s::numeric[],
                              %s::text[],%s::text[],%s::text[],%s::timestamptz[])
                      AS t(symbol,trading_date,open,high,low,close,pre_close,volume,amount,adj_factor,is_suspended,
                           limit_up,limit_down,selected_provider,source_ids_json,quality_status,available_at)
               ON CONFLICT(symbol,trading_date) DO UPDATE SET open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
                 close=EXCLUDED.close,pre_close=EXCLUDED.pre_close,volume=EXCLUDED.volume,amount=EXCLUDED.amount,
                 adj_factor=EXCLUDED.adj_factor,is_suspended=EXCLUDED.is_suspended,limit_up=EXCLUDED.limit_up,
                 limit_down=EXCLUDED.limit_down,selected_provider=EXCLUDED.selected_provider,
                 source_observation_ids=EXCLUDED.source_observation_ids,quality_status=EXCLUDED.quality_status,
                 available_at=EXCLUDED.available_at,canonicalized_at=now()""",
            (
                [key[0] for key in replace_keys], [key[1] for key in replace_keys],
                [row["open"] for row in rows], [row["high"] for row in rows], [row["low"] for row in rows],
                [row["close"] for row in rows], [row["pre_close"] for row in rows], [row["volume"] for row in rows],
                [row["amount"] for row in rows], [row["adj_factor"] for row in rows],
                [row["is_suspended"] for row in rows], [row["limit_up"] for row in rows],
                [row["limit_down"] for row in rows], [row["selected_provider"] for row in rows],
                [row["source_ids_json"] for row in rows], [row["quality_status"] for row in rows],
                [row["available_at"] for row in rows],
            ),
        )
    if update_only_keys:
        rows = [canonical_final[key] for key in update_only_keys]
        connection.execute(
            """UPDATE quant.canonical_bars_daily AS c
                  SET source_observation_ids=(SELECT array_agg(elem::uuid) FROM jsonb_array_elements_text(t.source_ids_json::jsonb) elem),
                      canonicalized_at=now()
                 FROM unnest(%s::text[],%s::date[],%s::text[]) AS t(symbol,trading_date,source_ids_json)
                WHERE c.symbol=t.symbol AND c.trading_date=t.trading_date""",
            (
                [key[0] for key in update_only_keys], [key[1] for key in update_only_keys],
                [row["source_ids_json"] for row in rows],
            ),
        )

    return len(mb_final)


__all__ = ["upsert_daily_bars"]
