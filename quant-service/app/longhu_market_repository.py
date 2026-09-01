"""Persistence boundary for the Longhu/Tencent full-market close source."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Callable

from psycopg.types.json import Json

from .longhu_market_sync import MergedCrossSection, PROVIDER_KEY, build_control_rows
from .universe_history import sync_universe_membership_history


def persist_settled_trade_calendar(
    connection: Any,
    trade_date: date,
    observed_at: datetime,
) -> int:
    """Record the session proven open by a coverage-gated settled close.

    A same-date, cross-checked all-A close is stronger evidence that the
    exchange opened than an absent calendar-provider row.  This projection is
    deliberately narrow: it records only the observed date and never invents
    future sessions.  A pre-existing Tushare calendar keeps its provider label.
    """
    stored = 0
    for exchange in ("SSE", "SZSE", "BSE"):
        prior = connection.execute(
            """SELECT max(calendar_date) AS prior_date
                 FROM quant.market_trade_calendar
                WHERE exchange=%s AND calendar_date<%s AND is_open""",
            (exchange, trade_date),
        ).fetchone()
        connection.execute(
            """INSERT INTO quant.market_trade_calendar(
                   exchange,calendar_date,is_open,pretrade_date,provider,available_at,raw)
               VALUES(%s,%s,true,%s,%s,%s,%s)
               ON CONFLICT(exchange,calendar_date) DO UPDATE SET
                 is_open=true,
                 pretrade_date=coalesce(quant.market_trade_calendar.pretrade_date,EXCLUDED.pretrade_date),
                 provider=CASE WHEN quant.market_trade_calendar.provider='tushare'
                               THEN quant.market_trade_calendar.provider ELSE EXCLUDED.provider END,
                 available_at=greatest(quant.market_trade_calendar.available_at,EXCLUDED.available_at),
                 raw=quant.market_trade_calendar.raw || EXCLUDED.raw""",
            (
                exchange, trade_date, prior["prior_date"] if prior else None, PROVIDER_KEY, observed_at,
                Json({
                    "derivation": "coverage_gated_settled_all_a_close",
                    "decision_boundary": "observed_date_only_no_future_calendar_inference",
                }),
            ),
        )
        stored += 1
    return stored


def persist_full_market_close(
    connection: Any,
    *,
    trade_date: date,
    request_key: str,
    observed_at: datetime,
    merged: MergedCrossSection,
    source_health: dict[str, Any],
    board_rows: list[dict[str, Any]],
    persist_rows: Callable[..., int],
    persist_flow_rows: Callable[..., int],
) -> dict[str, Any]:
    """Persist one coverage-gated cross-section in a caller-owned transaction."""
    stock_basic = [{
        "ts_code": row["ts_code"],
        "name": row.get("name"),
        "exchange": row["ts_code"].split(".")[1],
        "industry": None,
    } for row in merged.daily_rows]
    controls = build_control_rows(merged.daily_rows)
    calendar_rows = persist_settled_trade_calendar(connection, trade_date, observed_at)
    normalized = {
        "stock_basic": persist_rows(
            connection, "stock_basic", request_key + ":stock_basic", stock_basic, PROVIDER_KEY, observed_at,
        ),
        "daily": persist_rows(
            connection, "daily", request_key + ":daily", merged.daily_rows, PROVIDER_KEY, observed_at,
        ),
        "daily_basic": persist_rows(
            connection, "daily_basic", request_key + ":daily_basic", merged.fundamental_rows, PROVIDER_KEY, observed_at,
        ),
        "adj_factor": persist_rows(
            connection, "adj_factor", request_key + ":adj_factor", controls["adj_factor"], PROVIDER_KEY, observed_at,
        ),
        "stk_limit": persist_rows(
            connection, "stk_limit", request_key + ":stk_limit", controls["stk_limit"], PROVIDER_KEY, observed_at,
        ),
    }
    symbols = [row["ts_code"] for row in merged.daily_rows]
    connection.execute(
        """INSERT INTO quant.universe_members(universe_key,symbol,enabled,priority,source,metadata)
           SELECT 'all_a',candidate.symbol,true,20,%s,
                  jsonb_build_object('snapshot_date',%s::text,'coverage_gated',true)
             FROM unnest(%s::text[]) AS candidate(symbol)
           ON CONFLICT(universe_key,symbol) DO UPDATE SET enabled=true,priority=EXCLUDED.priority,
             source=EXCLUDED.source,metadata=EXCLUDED.metadata,updated_at=now()""",
        (PROVIDER_KEY, trade_date, symbols),
    )
    if symbols:
        connection.execute(
            """UPDATE quant.universe_members SET enabled=false,updated_at=now(),
                      metadata=metadata || jsonb_build_object('disabled_by_snapshot',%s::text)
                WHERE universe_key='all_a' AND enabled AND NOT (symbol=ANY(%s))""",
            (trade_date, symbols),
        )
    history = sync_universe_membership_history(
        connection, "all_a", trade_date, symbols, source=PROVIDER_KEY, priority=20,
    )
    flow_count = persist_flow_rows(connection, merged.flow_rows, PROVIDER_KEY, observed_at)
    quote_count = 0
    fetch_run = connection.execute(
        "SELECT fetch_run_id FROM quant.fetch_runs WHERE request_key=%s", (request_key,),
    ).fetchone()
    fetch_run_id = fetch_run["fetch_run_id"] if fetch_run else None
    for quote in merged.quote_rows:
        serialized = json.dumps(quote, ensure_ascii=False, sort_keys=True, default=str)
        connection.execute(
            """INSERT INTO quant.raw_market_observations(
                   provider_key,capability,market,symbol,effective_at,available_at,
                   availability_basis,payload_sha256,normalized,payload,fetch_run_id)
               VALUES(%s,'realtime_quote','cn',%s,%s,%s,'post_close_vendor_plus_public_crosscheck',%s,%s,%s,%s)
               ON CONFLICT(provider_key,capability,market,symbol,effective_at,payload_sha256)
               DO UPDATE SET available_at=EXCLUDED.available_at,fetch_run_id=EXCLUDED.fetch_run_id""",
            (PROVIDER_KEY, quote["ts_code"], observed_at, observed_at,
             hashlib.sha256(serialized.encode("utf-8")).hexdigest(), Json(quote), Json(quote), fetch_run_id),
        )
        quote_count += 1
    usable_boards = [row for row in board_rows if row.get("net_inflow") is not None]
    inflow = sorted(usable_boards, key=lambda row: float(row["net_inflow"]), reverse=True)[:10]
    outflow = sorted(usable_boards, key=lambda row: float(row["net_inflow"]))[:10]
    board_summary = {"longhu_ths_industry": {"inflow": inflow, "outflow": outflow}}
    prior_board_report = connection.execute(
        """SELECT board_report_id FROM quant.intraday_board_reports
             WHERE status='completed'
               AND (observed_at AT TIME ZONE 'Asia/Shanghai')::date=%s
               AND source_status->>'provider'=%s
             ORDER BY observed_at DESC LIMIT 1""",
        (trade_date, PROVIDER_KEY),
    ).fetchone()
    if prior_board_report:
        connection.execute(
            """UPDATE quant.intraday_board_reports SET observed_at=%s,source_status=%s,summary=%s,payload=%s
                WHERE board_report_id=%s""",
            (observed_at, Json({
                "provider": PROVIDER_KEY, "coverage": source_health,
                "flow_semantics": "order_size_classified_not_institution_identity",
            }), Json(board_summary), Json({
                "status": "completed", "items": board_rows,
                "coverage": {"board_count": len(board_rows), "usable_flow_count": len(usable_boards)},
                "source": PROVIDER_KEY,
            }), prior_board_report["board_report_id"]),
        )
    else:
        connection.execute(
            """INSERT INTO quant.intraday_board_reports(
                   observed_at,status,source_status,summary,payload)
               VALUES(%s,'completed',%s,%s,%s)""",
            (observed_at, Json({
                "provider": PROVIDER_KEY, "coverage": source_health,
                "flow_semantics": "order_size_classified_not_institution_identity",
            }), Json(board_summary), Json({
                "status": "completed", "items": board_rows,
                "coverage": {"board_count": len(board_rows), "usable_flow_count": len(usable_boards)},
                "source": PROVIDER_KEY,
            })),
        )
    connection.execute(
        """UPDATE quant.fetch_runs SET status='completed',row_count=%s,finished_at=now(),
                  metadata=metadata || %s::jsonb
            WHERE request_key=%s""",
        (len(merged.daily_rows), Json({
            "coverage": merged.coverage, "normalized": normalized,
            "flow_rows": flow_count, "quote_rows": quote_count, "board_rows": len(board_rows),
            "source_health": source_health, "close_conflicts": list(merged.close_conflicts[:20]),
            "control_semantics": {
                "adj_factor": "same_day_identity_only",
                "stk_limit": "derived_from_preclose_board_rule_with_exception_warning",
                "trade_calendar": "observed_open_from_coverage_gated_settled_close",
            },
        }), request_key),
    )
    for capability, row_count, note in (
        ("daily", len(merged.daily_rows), "Coverage-gated all-A post-close daily cross-section verified."),
        ("stock_money_flow", flow_count, "Vendor field 13 order-size-classified main-net cross-section verified."),
        ("realtime_quote", quote_count, "Same-session Tencent OHLC cross-check verified."),
    ):
        connection.execute(
            """INSERT INTO quant.provider_api_capabilities(
                   provider_key,api_name,availability,frequency,decision_eligible,note,verified_at,metadata)
               VALUES(%s,%s,'verified','post_close',true,%s,now(),%s)
               ON CONFLICT(provider_key,api_name) DO UPDATE SET
                 availability='verified',frequency='post_close',decision_eligible=true,
                 note=EXCLUDED.note,verified_at=now(),last_checked_at=now(),metadata=EXCLUDED.metadata""",
            (PROVIDER_KEY, capability, note, Json({"row_count": row_count, "trade_date": str(trade_date)})),
        )
        connection.execute(
            """INSERT INTO quant.provider_health(
                   provider_key,capability,market,consecutive_failures,last_success_at,last_row_count,updated_at)
               VALUES(%s,%s,'cn',0,now(),%s,now())
               ON CONFLICT(provider_key,capability,market) DO UPDATE SET
                 consecutive_failures=0,circuit_open_until=NULL,last_success_at=now(),
                 last_error=NULL,last_row_count=EXCLUDED.last_row_count,updated_at=now()""",
            (PROVIDER_KEY, capability, row_count),
        )
    return {
        "daily_rows": len(merged.daily_rows), "flow_rows": flow_count,
        "quote_rows": quote_count, "board_rows": len(board_rows), "coverage": merged.coverage,
        "normalized": normalized, "universe_history": history,
        "calendar_rows": calendar_rows,
        "close_conflicts": len(merged.close_conflicts),
    }


def persisted_close_context(database: Any, trade_date: date) -> dict[str, Any]:
    """Describe the already-saved Longhu close without fetching a provider."""
    with database.transaction() as connection:
        counts = connection.execute(
            """SELECT
                 (SELECT count(DISTINCT symbol)::int FROM quant.canonical_bars_daily
                   WHERE trading_date=%s AND selected_provider=%s) AS daily_rows,
                 (SELECT count(DISTINCT symbol)::int FROM quant.stock_money_flow_daily
                   WHERE trading_date=%s AND provider=%s AND source='longhuvip_main_net') AS flow_rows""",
            (trade_date, PROVIDER_KEY, trade_date, PROVIDER_KEY),
        ).fetchone()
        board = connection.execute(
            """SELECT observed_at,status,
                      jsonb_array_length(coalesce(payload->'items','[]'::jsonb)) AS board_rows,
                      source_status
                 FROM quant.intraday_board_reports
                WHERE status='completed'
                  AND (observed_at AT TIME ZONE 'Asia/Shanghai')::date=%s
                  AND source_status->>'provider'=%s
                ORDER BY observed_at DESC LIMIT 1""",
            (trade_date, PROVIDER_KEY),
        ).fetchone()
    daily_rows = int(counts["daily_rows"] or 0)
    flow_rows = int(counts["flow_rows"] or 0)
    board_rows = int(board["board_rows"] or 0) if board else 0
    ready = daily_rows >= 1000 and flow_rows >= 1000 and board_rows > 0
    return {
        "status": "completed" if ready else "blocked",
        "provider": PROVIDER_KEY,
        "trade_date": str(trade_date),
        "daily_rows": daily_rows,
        "flow_rows": flow_rows,
        "board_rows": board_rows,
        "observed_at": board["observed_at"] if board else None,
        "reason": None if ready else "saved Longhu close coverage is incomplete",
        "provider_calls": 0,
    }


__all__ = ["persist_full_market_close", "persist_settled_trade_calendar", "persisted_close_context"]
