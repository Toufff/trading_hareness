"""Persistence boundary for the Longhu/Tencent full-market close source."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from .longhu_market_sync import MergedCrossSection, PROVIDER_KEY, build_control_rows
from .universe_history import sync_universe_membership_history


LONGHU_INDUSTRY_TAXONOMY = "longhu_ths_industry"


def persist_longhu_industry_memberships(
    connection: Any,
    trade_date: date,
    observed_at: datetime,
    flow_rows: list[dict[str, Any]],
) -> int:
    """Materialise the industry carried by the same dated Longhu stock row.

    This is a complete all-A observed snapshot, not a reconstructed historical
    interval.  Keeping the effective/known dates explicit prevents today's
    classification from leaking backwards into a replay.
    """
    members: dict[str, tuple[str, str, dict[str, Any]]] = {}
    sectors: dict[str, str] = {}
    for row in flow_rows:
        raw = dict(row.get("raw") or {})
        sector_key = str(raw.get("plate_id") or "").strip()
        screen = raw.get("screen_snapshot") if isinstance(raw.get("screen_snapshot"), dict) else {}
        label = str(screen.get("sector_label") or raw.get("sector_label") or "").strip()
        symbol = str(row.get("symbol") or "").upper()
        if not sector_key or not label or not symbol:
            continue
        sectors[sector_key] = label
        members[symbol] = (sector_key, label, raw)
    if not members:
        return 0
    connection.execute(
        """INSERT INTO quant.sector_taxonomies(taxonomy_key,label,provider_key,metadata)
           VALUES(%s,%s,%s,%s)
           ON CONFLICT(taxonomy_key) DO UPDATE SET label=EXCLUDED.label,
             provider_key=EXCLUDED.provider_key,metadata=EXCLUDED.metadata,updated_at=now()""",
        (LONGHU_INDUSTRY_TAXONOMY, "同花顺行业（Longhu日终）", PROVIDER_KEY,
         Json({"basis": "complete_observed_close_cross_section"})),
    )
    connection.execute(
        """INSERT INTO quant.sectors(taxonomy_key,sector_key,label,metadata)
           SELECT %(taxonomy)s,t.sector_key,t.label,
                  jsonb_build_object('provider',%(provider)s,'observed_date',%(trade_date)s::text)
             FROM unnest(%(keys)s::text[],%(labels)s::text[]) AS t(sector_key,label)
           ON CONFLICT(taxonomy_key,sector_key) DO UPDATE SET label=EXCLUDED.label,
             metadata=quant.sectors.metadata || EXCLUDED.metadata,updated_at=now()""",
        {"taxonomy": LONGHU_INDUSTRY_TAXONOMY, "provider": PROVIDER_KEY,
         "trade_date": trade_date, "keys": list(sectors), "labels": list(sectors.values())},
    )
    symbols = list(members)
    connection.execute(
        """INSERT INTO quant.sector_membership_history(
               taxonomy_key,sector_key,symbol,effective_from,effective_to,provider_key,
               available_at,known_at,effective_from_basis,effective_to_basis,raw)
           SELECT %(taxonomy)s,t.sector_key,t.symbol,%(trade_date)s,NULL,%(provider)s,
                  %(observed_at)s,%(observed_at)s,'observed_snapshot','observed_snapshot',t.raw::jsonb
             FROM unnest(%(sector_keys)s::text[],%(symbols)s::text[],%(raws)s::text[])
                  AS t(sector_key,symbol,raw)
           ON CONFLICT(taxonomy_key,sector_key,symbol,effective_from) DO UPDATE SET
             effective_to=NULL,provider_key=EXCLUDED.provider_key,available_at=EXCLUDED.available_at,
             known_at=EXCLUDED.known_at,effective_from_basis=EXCLUDED.effective_from_basis,
             effective_to_basis=EXCLUDED.effective_to_basis,raw=EXCLUDED.raw""",
        {"taxonomy": LONGHU_INDUSTRY_TAXONOMY, "trade_date": trade_date,
         "provider": PROVIDER_KEY, "observed_at": observed_at,
         "sector_keys": [members[symbol][0] for symbol in symbols], "symbols": symbols,
         "raws": [json.dumps(members[symbol][2], ensure_ascii=False, default=str) for symbol in symbols]},
    )
    connection.execute(
        """UPDATE quant.sector_membership_history prior SET
               effective_to=%s,available_at=%s,known_at=%s,effective_to_basis='observed_snapshot'
             WHERE prior.taxonomy_key=%s AND prior.provider_key=%s AND prior.effective_to IS NULL
               AND prior.effective_from<%s
               AND NOT EXISTS (
                 SELECT 1 FROM unnest(%s::text[],%s::text[]) current(sector_key,symbol)
                  WHERE current.sector_key=prior.sector_key AND current.symbol=prior.symbol
               )""",
        (trade_date - timedelta(days=1), observed_at, observed_at,
         LONGHU_INDUSTRY_TAXONOMY, PROVIDER_KEY, trade_date,
         [members[symbol][0] for symbol in symbols], symbols),
    )
    return len(members)


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
    # A coverage-gated quote snapshot can still miss suspended stocks or
    # individual upstream failures. Missing prices are not a delisting event.
    history = sync_universe_membership_history(
        connection, "all_a", trade_date, symbols, source=PROVIDER_KEY, priority=20,
        close_missing=False,
    )
    flow_count = persist_flow_rows(connection, merged.flow_rows, PROVIDER_KEY, observed_at)
    industry_membership_count = persist_longhu_industry_memberships(
        connection, trade_date, observed_at, merged.flow_rows,
    )
    fetch_run = connection.execute(
        "SELECT fetch_run_id FROM quant.fetch_runs WHERE request_key=%s", (request_key,),
    ).fetchone()
    fetch_run_id = fetch_run["fetch_run_id"] if fetch_run else None
    quote_count = len(merged.quote_rows)
    if merged.quote_rows:
        # One set-based upsert instead of one INSERT per quote row (a full
        # close cross-section is one row per A-share symbol, ~5,500 rows).
        # Deduplicated by (symbol, payload_sha256), last one wins, because
        # PostgreSQL rejects an ON CONFLICT DO UPDATE that would affect the
        # same target row twice within a single statement.
        deduplicated: dict[tuple[str, str], str] = {}
        for quote in merged.quote_rows:
            serialized = json.dumps(quote, ensure_ascii=False, sort_keys=True, default=str)
            content_sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            deduplicated[(quote["ts_code"], content_sha256)] = serialized
        symbols = [key[0] for key in deduplicated]
        shas = [key[1] for key in deduplicated]
        payloads = list(deduplicated.values())
        connection.execute(
            """INSERT INTO quant.raw_market_observations(
                   provider_key,capability,market,symbol,effective_at,available_at,
                   availability_basis,payload_sha256,normalized,payload,fetch_run_id)
               SELECT %(provider)s,'settled_quote','cn',t.symbol,%(effective_at)s,%(observed_at)s,
                      'dated_licensed_close_crosscheck',t.sha,t.payload_json::jsonb,t.payload_json::jsonb,%(fetch_run_id)s
                 FROM unnest(%(symbols)s::text[],%(shas)s::text[],%(payloads)s::text[]) AS t(symbol,sha,payload_json)
               ON CONFLICT(provider_key,capability,market,symbol,effective_at,payload_sha256)
               DO UPDATE SET available_at=EXCLUDED.available_at,fetch_run_id=EXCLUDED.fetch_run_id""",
            {"provider": PROVIDER_KEY, "observed_at": observed_at, "fetch_run_id": fetch_run_id,
             "effective_at": datetime.combine(trade_date, datetime.min.time(), ZoneInfo('Asia/Shanghai')).replace(hour=15),
             "symbols": symbols, "shas": shas, "payloads": payloads},
        )
    usable_boards = [row for row in board_rows if row.get("net_inflow") is not None]
    inflow = sorted(usable_boards, key=lambda row: float(row["net_inflow"]), reverse=True)[:10]
    outflow = sorted(usable_boards, key=lambda row: float(row["net_inflow"]))[:10]
    board_summary = {"longhu_ths_industry": {"inflow": inflow, "outflow": outflow}}
    prior_board_report = connection.execute(
        """SELECT board_report_id FROM quant.intraday_board_reports
             WHERE status='completed'
               AND coalesce(source_status->>'trade_date',source_status->'coverage'->'licensed_ohlc'->>'trade_date',(observed_at AT TIME ZONE 'Asia/Shanghai')::date::text)=%s::text
               AND source_status->>'provider'=%s
             ORDER BY observed_at DESC LIMIT 1""",
        (trade_date, PROVIDER_KEY),
    ).fetchone()
    if prior_board_report:
        connection.execute(
            """UPDATE quant.intraday_board_reports SET observed_at=%s,source_status=%s,summary=%s,payload=%s
                WHERE board_report_id=%s""",
            (observed_at, Json({
                "provider": PROVIDER_KEY, "coverage": source_health, "trade_date": str(trade_date),
                "data_as_of": f"{trade_date}T15:00:00+08:00",
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
                "provider": PROVIDER_KEY, "coverage": source_health, "trade_date": str(trade_date),
                "data_as_of": f"{trade_date}T15:00:00+08:00",
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
            "industry_memberships": industry_membership_count,
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
        ("settled_quote", quote_count, "Licensed dated OHLC cross-check verified; not realtime."),
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
        "industry_memberships": industry_membership_count,
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
                  AND coalesce(source_status->>'trade_date',source_status->'coverage'->'licensed_ohlc'->>'trade_date',(observed_at AT TIME ZONE 'Asia/Shanghai')::date::text)=%s::text
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


__all__ = [
    "persist_full_market_close", "persist_longhu_industry_memberships",
    "persist_settled_trade_calendar", "persisted_close_context",
]
