"""Set-based batch upsert for canonical daily bars.

``daily_bar_repository.upsert_daily_bar`` runs 5-6 statements per bar,
including a per-bar ``SELECT ... FOR UPDATE``-less read-then-write against
``quant.canonical_bars_daily``.  Called once per symbol for a full A-share
cross-section (~5,500 symbols), that is ~30,000 statements in a single
transaction.  ``upsert_daily_bars`` here batches the same write contract
(provider-priority selection, immutable raw evidence, amount-unit
quarantine) into a small, fixed number of
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
from .instrument_lock_retry import execute_instrument_write
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
    # Ascending symbol order, not payload/dict-insertion order.  This is the
    # strongest ``quant.instruments`` lock in the platform: ``ON CONFLICT DO
    # UPDATE`` row-locks every EXISTING conflicting row -- the whole
    # cross-section on any day after the first -- where the ``DO NOTHING``
    # registration in ``instrument_registry`` locks only genuinely new rows.
    # Two concurrent ``normalize_tushare_rows`` transactions (a 'daily'
    # cross-section and an 'index_daily'/partial refresh sharing symbols)
    # deadlock here unless both take the rows in the same order -- that is
    # the 2026-09-18 cycle.  Keep this ``sorted``: it is the same global
    # order ``instrument_registry.normalized_symbols`` uses.  The statement
    # below also carries ``ORDER BY 1``: the array is already ascending, so
    # the server sort is free, and it is what makes "every writer sorts in
    # the statement" checkable by a test rather than by reading each loop.
    for symbol, entry in sorted(instrument_entries.items()):
        old = existing_instruments.get(symbol)
        inst_symbols.append(symbol)
        inst_exchanges.append(exchange_for(symbol))
        inst_names.append(entry["name"] if entry["name"] is not None else (old["name"] if old else None))
        inst_industries.append(entry["industry"] if entry["industry"] is not None else (old["industry"] if old else None))
        inst_is_st.append(entry["is_st"] if entry["is_st"] is not None else (old["is_st"] if old else False))
        inst_sources.append(entry["source"])
    execute_instrument_write(
        connection,
        """INSERT INTO quant.instruments(symbol,exchange,name,industry,is_st,source)
           SELECT * FROM unnest(%s::text[],%s::text[],%s::text[],%s::text[],%s::boolean[],%s::text[])
           ORDER BY 1
           ON CONFLICT(symbol) DO UPDATE SET exchange=EXCLUDED.exchange,name=EXCLUDED.name,
             industry=EXCLUDED.industry,is_st=EXCLUDED.is_st,source=EXCLUDED.source,updated_at=now()""",
        (inst_symbols, inst_exchanges, inst_names, inst_industries, inst_is_st, inst_sources),
        writer="daily_bar_batch_repository.upsert_daily_bars",
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
    # ``RETURNING`` can only project columns of the target table, so the
    # caller-side ``row_index`` cannot be selected out of the INSERT itself
    # (doing so fails with `column "row_index" does not exist`, which aborted
    # the whole transaction and blocked every post-close daily refresh). Keep
    # the input as a CTE and join the inserted rows back onto it by the
    # conflict key instead; ``capability`` and ``market`` are constants here,
    # so the remaining four columns identify a row uniquely. ``DO UPDATE``
    # rather than ``DO NOTHING`` matters: it makes conflicting rows come back
    # from RETURNING too, so every input bar gets its observation id.
    observation_rows = connection.execute(
        """WITH input AS (
               SELECT * FROM unnest(%s::text[],%s::text[],%s::timestamptz[],%s::timestamptz[],%s::text[],%s::text[],%s::integer[])
                    AS t(provider_key,symbol,effective_at,available_at,payload_sha256,normalized_json,row_index)
           ), inserted AS (
               INSERT INTO quant.raw_market_observations(provider_key,capability,market,symbol,effective_at,available_at,payload_sha256,normalized,payload)
               SELECT i.provider_key,'daily_bar','cn',i.symbol,i.effective_at,i.available_at,i.payload_sha256,
                      i.normalized_json::jsonb,i.normalized_json::jsonb
                 FROM input i
               ON CONFLICT(provider_key,capability,market,symbol,effective_at,payload_sha256) DO UPDATE SET available_at=EXCLUDED.available_at
               RETURNING observation_id,provider_key,symbol,effective_at,payload_sha256
           )
           SELECT i.row_index,ins.observation_id
             FROM inserted ins
             JOIN input i
               ON i.provider_key=ins.provider_key AND i.symbol=ins.symbol
              AND i.effective_at=ins.effective_at AND i.payload_sha256=ins.payload_sha256""",
        (provider_arr, symbol_arr, effective_arr, avail_arr, sha_arr, normalized_arr, index_arr),
    ).fetchall()
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
    # Ascending ``(symbol, trading_date)``, not dict-insertion (= provider
    # payload) order, for the same reason the instruments array above is
    # sorted: this statement is ``ON CONFLICT DO UPDATE``, so it row-locks
    # every EXISTING ``quant.market_bars_daily`` row it touches, and
    # ``(symbol, trading_date)`` is the conflict key those locks are taken
    # on.  Two ingestion transactions covering overlapping symbols in
    # different payload orders is the 2026-09-18 deadlock cycle, one table
    # further down.  ``in_instrument_lock_order``'s docstring names these two
    # tables as the reason its key carries ``trading_date`` at all; the key
    # list is sorted ONCE here and every array below is built from it, so the
    # rows cannot drift apart and no stored value moves (``mb_final`` has
    # already resolved last-write-wins per key).
    keys = sorted(mb_final)
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

    # --- canonical_bars_daily: fold every bar for a key in input order ---
    # Taking only the last bar per key and comparing it against the pre-batch
    # row silently loses the provider-priority contract whenever one batch
    # carries two providers for the same (symbol, trading_date): the later,
    # lower-priority close would win where a sequential upsert_daily_bar loop
    # keeps the higher-priority one. Folding reproduces that loop exactly -
    # each bar is weighed against the selection in force at its turn, not
    # against the state the batch started from.
    indexes_by_key: dict[tuple[str, Any], list[int]] = {}
    for index, bar in enumerate(bars):
        indexes_by_key.setdefault((bar.symbol, bar.trading_date), []).append(index)

    replace_keys: list[tuple[str, Any]] = []
    update_only_keys: list[tuple[str, Any]] = []
    canonical_final: dict[tuple[str, Any], dict[str, Any]] = {}
    # A close conflict is judged against the canonical close in force when the
    # bar is processed, which inside one batch is the close an earlier bar of
    # the same batch just won with - not the pre-batch row, which for a first
    # import does not exist at all and would report no conflict ever.
    conflict_symbols: list[str] = []
    conflict_dates: list[Any] = []
    conflict_details: list[str] = []
    for key, indexes in indexes_by_key.items():
        existing = existing_canonical.get(key)
        merged_source_ids = ([str(value) for value in (existing["source_observation_ids"] or [])] if existing else [])
        selected_provider = str(existing["selected_provider"]) if existing else None
        selected_close = Decimal(existing["close"]) if existing and existing["close"] else None
        winning_index: int | None = None
        for index in indexes:
            merged_source_ids.append(str(observation_id_by_index[index]))
            bar = bars[index]
            if selected_close is not None and abs(selected_close - bar.close) > Decimal("0.001"):
                conflict_symbols.append(bar.symbol)
                conflict_dates.append(bar.trading_date)
                conflict_details.append(json.dumps({
                    "existing_provider": selected_provider, "existing_close": str(selected_close),
                    "incoming_provider": bar.source, "incoming_close": str(bar.close),
                }))
            if selected_provider is None or provider_priority(bar.source) <= provider_priority(selected_provider):
                selected_provider = bar.source
                selected_close = bar.close
                winning_index = index
        source_ids_json = json.dumps(merged_source_ids)
        if winning_index is None:
            # Every bar in this batch lost to the provider already on record;
            # only the evidence trail grows.
            canonical_final[key] = {"source_ids_json": source_ids_json}
            update_only_keys.append(key)
            continue
        bar = bars[winning_index]
        canonical_final[key] = {
            "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close, "pre_close": bar.pre_close,
            "volume": bar.volume, "amount": promoted_amount[winning_index],
            "adj_factor": bar.adj_factor if bar.adj_factor is not None else (existing["adj_factor"] if existing else None),
            "is_suspended": bar.is_suspended if bar.is_suspended is not None else (existing["is_suspended"] if existing else False),
            "limit_up": bar.limit_up if bar.limit_up is not None else (existing["limit_up"] if existing else None),
            "limit_down": bar.limit_down if bar.limit_down is not None else (existing["limit_down"] if existing else None),
            "selected_provider": bar.source,
            "source_ids_json": source_ids_json,
            "quality_status": "partial" if amount_mismatch[winning_index] else "fresh",
            "available_at": available_at_utc[winning_index],
        }
        replace_keys.append(key)

    # The same ascending lock order for ``quant.canonical_bars_daily``: the
    # INSERT below is ``ON CONFLICT DO UPDATE`` and the UPDATE below it locks
    # the rows it matches, both keyed on ``(symbol, trading_date)``.  The two
    # lists are disjoint and each is sorted once, before any array is built
    # from it, so ordering changes which lock is taken first and nothing
    # else -- ``canonical_final`` already holds the folded result per key.
    replace_keys.sort()
    update_only_keys.sort()

    if conflict_symbols:
        connection.execute(
            """INSERT INTO quant.data_quality_issues(capability,symbol,trading_date,severity,code,message,details)
               SELECT 'daily_bar',t.symbol,t.trading_date,'warning','provider_close_conflict',
                      'daily close differs across providers',t.details::jsonb
                 FROM unnest(%s::text[],%s::date[],%s::text[]) AS t(symbol,trading_date,details)""",
            (conflict_symbols, conflict_dates, conflict_details),
        )

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
