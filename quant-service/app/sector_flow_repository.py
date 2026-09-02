"""Materialize daily THS sector-flow migration with exact-member LHB context."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Any

from .sector_flow_features import sector_flow_feature, sector_flow_outcome
from .sector_membership_repository import point_in_time_membership_predicate


def _sign(value: Any) -> int:
    number = float(value or 0)
    return 1 if number > 0 else -1 if number < 0 else 0


def rebuild_sector_flow_daily_features(database: Any, start_date: date, end_date: date) -> dict[str, Any]:
    if end_date < start_date or (end_date - start_date).days > 45:
        raise ValueError("sector-flow feature rebuild requires an ordered range capped at 45 days")
    context_start = start_date - timedelta(days=10)
    membership_predicate_lhb = point_in_time_membership_predicate("m", "l.day")
    membership_predicate_event = point_in_time_membership_predicate("m", "event.day")
    with database.transaction() as connection:
        flow_rows = connection.execute(
            """SELECT DISTINCT ON(trading_date,sector_key)
                      trading_date,sector_key,provider_key,available_at,change_pct,net_amount,
                      net_buy_amount,net_sell_amount
                 FROM quant.sector_market_observations
                WHERE taxonomy_key='ths_concept_flow' AND trading_date BETWEEN %s AND %s
                ORDER BY trading_date,sector_key,
                         CASE provider_key WHEN 'tushare_super_sdk' THEN 0
                                           WHEN 'tushare_super_get' THEN 1 ELSE 2 END,
                         available_at DESC""",
            (context_start, end_date),
        ).fetchall()
        lhb_rows = connection.execute(
            f"""WITH event_json AS (
                   SELECT occurred_at,symbol,available_at,
                          CASE WHEN body IS JSON THEN body::jsonb END AS payload
                     FROM quant.market_events WHERE event_type='lhb_event'
               ), lhb_candidates AS (
                   SELECT (occurred_at AT TIME ZONE 'Asia/Shanghai')::date AS day,symbol,
                          COALESCE(NULLIF(payload->>'龙虎榜净买额','')::numeric,0) AS net_amount,
                          COALESCE(NULLIF(payload->>'龙虎榜成交额','')::numeric,0) AS lhb_amount,
                          1 AS source_priority,available_at
                     FROM event_json WHERE payload IS NOT NULL
                   UNION ALL
                   SELECT to_date(row_data->>'trade_date','YYYYMMDD') AS day,row_data->>'ts_code' AS symbol,
                          COALESCE(NULLIF(row_data->>'net_amount','')::numeric,0) AS net_amount,
                          COALESCE(NULLIF(row_data->>'l_amount','')::numeric,0) AS lhb_amount,
                          0 AS source_priority,available_at
                     FROM quant.tushare_raw_records WHERE api_name='top_list'
               ), one_per_symbol AS (
                   SELECT DISTINCT ON(day,symbol) day,symbol,net_amount
                     FROM lhb_candidates WHERE day BETWEEN %s AND %s
                    ORDER BY day,symbol,source_priority,lhb_amount DESC,available_at DESC
               ), aggregate AS (
                   SELECT l.day,m.sector_key,count(*) AS stock_count,
                          sum(l.net_amount) AS net_amount,
                          count(*) FILTER (WHERE l.net_amount<0) AS negative_count
                     FROM one_per_symbol l
                     JOIN quant.sector_membership_history m
                       ON m.taxonomy_key='ths_concept_flow' AND m.symbol=l.symbol
                      AND {membership_predicate_lhb}
                    GROUP BY l.day,m.sector_key
               ), limits AS (
                   SELECT day,m.sector_key,count(DISTINCT event.symbol) AS limit_up_count
                     FROM (
                         SELECT symbol,(occurred_at AT TIME ZONE 'Asia/Shanghai')::date AS day
                           FROM quant.market_events WHERE event_type='limit_up_pool'
                     ) event
                     JOIN quant.sector_membership_history m
                       ON m.taxonomy_key='ths_concept_flow' AND m.symbol=event.symbol
                      AND {membership_predicate_event}
                    WHERE day BETWEEN %s AND %s GROUP BY day,m.sector_key
               )
               SELECT COALESCE(a.day,l.day) AS trading_date,
                      COALESCE(a.sector_key,l.sector_key) AS sector_key,
                      COALESCE(a.stock_count,0) AS stock_count,
                      COALESCE(a.net_amount,0) AS net_amount,
                      COALESCE(a.negative_count,0) AS negative_count,
                      COALESCE(l.limit_up_count,0) AS limit_up_count
                 FROM aggregate a FULL JOIN limits l USING(day,sector_key)""",
            (start_date, end_date, start_date, end_date),
        ).fetchall()

    flows_by_sector: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for raw in flow_rows:
        row = dict(raw)
        flows_by_sector[str(row["sector_key"])].append(row)
        by_day[row["trading_date"]].append(row)
    lhb_map = {(row["trading_date"], str(row["sector_key"])): dict(row) for row in lhb_rows}
    rank_map: dict[tuple[date, str], float | None] = {}
    for day, rows in by_day.items():
        ranked = sorted((row for row in rows if row["net_amount"] is not None), key=lambda row: float(row["net_amount"]))
        denominator = max(1, len(ranked) - 1)
        for index, row in enumerate(ranked):
            rank_map[(day, str(row["sector_key"]))] = index / denominator

    stored = 0
    transitions: dict[str, int] = {}
    to_write: list[dict[str, Any]] = []
    for sector_key, rows in flows_by_sector.items():
        rows.sort(key=lambda row: row["trading_date"])
        streak = 0
        last_sign = 0
        for index, row in enumerate(rows):
            current_sign = _sign(row["net_amount"])
            streak = streak + current_sign if current_sign and current_sign == last_sign else current_sign
            last_sign = current_sign
            if row["trading_date"] < start_date:
                continue
            feature = sector_flow_feature(
                row,
                previous=rows[index - 1] if index >= 1 else None,
                prior=rows[index - 2] if index >= 2 else None,
                rank_percentile=rank_map.get((row["trading_date"], sector_key)),
                sign_streak=streak,
                lhb=lhb_map.get((row["trading_date"], sector_key)),
            )
            quality_flags = ["previous_flow_missing"] if index == 0 else []
            to_write.append({
                "sector_key": sector_key, "trading_date": row["trading_date"], "provider_key": row["provider_key"],
                "available_at": row["available_at"], "status": "partial" if quality_flags else "ready",
                "change_pct": row["change_pct"], "feature": feature, "quality_flags": quality_flags,
            })
            stored += 1
            transitions[feature["transition"]] = transitions.get(feature["transition"], 0) + 1
    if to_write:
        # One set-based upsert instead of one INSERT per (sector, day) row.
        connection_batch = (
            [item["sector_key"] for item in to_write],
            [item["trading_date"] for item in to_write],
            [item["provider_key"] for item in to_write],
            [item["available_at"] for item in to_write],
            [item["status"] for item in to_write],
            [item["feature"]["transition"] for item in to_write],
            [item["feature"]["net_amount"] for item in to_write],
            [item["feature"]["previous_net_amount"] for item in to_write],
            [item["feature"]["net_change_amount"] for item in to_write],
            [item["feature"]["net_acceleration"] for item in to_write],
            [item["feature"]["rank_percentile"] for item in to_write],
            [item["feature"]["flow_sign_streak"] for item in to_write],
            [item["change_pct"] for item in to_write],
            [item["feature"]["price_flow_divergence"] for item in to_write],
            [item["feature"]["lhb_stock_count"] for item in to_write],
            [item["feature"]["lhb_net_amount"] for item in to_write],
            [item["feature"]["lhb_negative_count"] for item in to_write],
            [item["feature"]["lhb_sell_pressure_ratio"] for item in to_write],
            [item["feature"]["limit_up_count"] for item in to_write],
            [json.dumps(item["feature"], default=str) for item in to_write],
            [json.dumps(item["quality_flags"], default=str) for item in to_write],
        )
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO quant.sector_flow_daily_features(
                       taxonomy_key,sector_key,trading_date,provider_key,available_at,status,transition,
                       net_amount,previous_net_amount,net_change_amount,net_acceleration,rank_percentile,
                       flow_sign_streak,change_pct,price_flow_divergence,lhb_stock_count,lhb_net_amount,
                       lhb_negative_count,lhb_sell_pressure_ratio,limit_up_count,features,quality_flags)
                   SELECT 'ths_concept_flow',t.sector_key,t.trading_date,t.provider_key,t.available_at,t.status,
                          t.transition,t.net_amount,t.previous_net_amount,t.net_change_amount,t.net_acceleration,
                          t.rank_percentile,t.flow_sign_streak,t.change_pct,t.price_flow_divergence,
                          t.lhb_stock_count,t.lhb_net_amount,t.lhb_negative_count,t.lhb_sell_pressure_ratio,
                          t.limit_up_count,t.features::jsonb,t.quality_flags::jsonb
                     FROM unnest(%s::text[],%s::date[],%s::text[],%s::timestamptz[],%s::text[],%s::text[],
                                  %s::numeric[],%s::numeric[],%s::numeric[],%s::numeric[],%s::numeric[],%s::int[],
                                  %s::numeric[],%s::numeric[],%s::int[],%s::numeric[],%s::int[],%s::numeric[],
                                  %s::int[],%s::text[],%s::text[])
                          AS t(sector_key,trading_date,provider_key,available_at,status,transition,
                               net_amount,previous_net_amount,net_change_amount,net_acceleration,rank_percentile,
                               flow_sign_streak,change_pct,price_flow_divergence,lhb_stock_count,lhb_net_amount,
                               lhb_negative_count,lhb_sell_pressure_ratio,limit_up_count,features,quality_flags)
                   ON CONFLICT(taxonomy_key,sector_key,trading_date) DO UPDATE SET
                     provider_key=EXCLUDED.provider_key,available_at=EXCLUDED.available_at,status=EXCLUDED.status,
                     transition=EXCLUDED.transition,net_amount=EXCLUDED.net_amount,
                     previous_net_amount=EXCLUDED.previous_net_amount,net_change_amount=EXCLUDED.net_change_amount,
                     net_acceleration=EXCLUDED.net_acceleration,rank_percentile=EXCLUDED.rank_percentile,
                     flow_sign_streak=EXCLUDED.flow_sign_streak,change_pct=EXCLUDED.change_pct,
                     price_flow_divergence=EXCLUDED.price_flow_divergence,lhb_stock_count=EXCLUDED.lhb_stock_count,
                     lhb_net_amount=EXCLUDED.lhb_net_amount,lhb_negative_count=EXCLUDED.lhb_negative_count,
                     lhb_sell_pressure_ratio=EXCLUDED.lhb_sell_pressure_ratio,limit_up_count=EXCLUDED.limit_up_count,
                     features=EXCLUDED.features,quality_flags=EXCLUDED.quality_flags,updated_at=now()""",
                connection_batch,
            )
    outcomes = materialize_sector_flow_daily_outcomes(database, end_date)
    return {
        "status": "completed", "start_date": str(start_date), "end_date": str(end_date),
        "stored": stored, "transition_counts": transitions, "source": "stored_evidence_only",
        "provider_calls": 0, "outcomes": outcomes, "decision_eligible": False,
    }


#: How far back ``materialize_sector_flow_daily_outcomes`` reads to settle
#: responses.  The read previously had no lower bound at all and rescanned
#: the entire history of both source tables on every call; the longest
#: horizon is 5 sessions, so a generous multi-month window is more than
#: enough context while keeping the read bounded.  Configurable per call.
DEFAULT_OUTCOME_LOOKBACK_DAYS = 120


def materialize_sector_flow_daily_outcomes(
    database: Any, as_of_date: date, *, lookback_days: int = DEFAULT_OUTCOME_LOOKBACK_DAYS,
) -> dict[str, Any]:
    """Settle 1/3/5-observation close responses using only stored daily rows."""
    lower_bound = as_of_date - timedelta(days=lookback_days)
    with database.transaction() as connection:
        feature_rows = connection.execute(
            """SELECT taxonomy_key,sector_key,trading_date,transition
                 FROM quant.sector_flow_daily_features
                WHERE taxonomy_key='ths_concept_flow' AND trading_date<=%s AND trading_date>=%s
                ORDER BY sector_key,trading_date""",
            (as_of_date, lower_bound),
        ).fetchall()
        price_rows = connection.execute(
            """SELECT DISTINCT ON(trading_date,sector_key) trading_date,sector_key,close,available_at
                 FROM quant.sector_market_observations
                WHERE taxonomy_key='ths_concept_flow' AND trading_date<=%s AND trading_date>=%s AND close>0
                ORDER BY trading_date,sector_key,
                         CASE provider_key WHEN 'tushare_super_sdk' THEN 0
                                           WHEN 'tushare_super_get' THEN 1 ELSE 2 END,
                         available_at DESC""",
            (as_of_date, lower_bound),
        ).fetchall()
    prices: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in price_rows:
        prices[str(raw["sector_key"])].append(dict(raw))
    for rows in prices.values():
        rows.sort(key=lambda row: row["trading_date"])
    provisional: list[dict[str, Any]] = []
    horizons = (1, 3, 5)
    for raw in feature_rows:
        feature = dict(raw)
        series = prices.get(str(feature["sector_key"]), [])
        position = next((index for index, row in enumerate(series) if row["trading_date"] == feature["trading_date"]), None)
        for horizon in horizons:
            item = {
                **feature, "horizon_days": horizon, "entry_date": feature["trading_date"],
                "entry_close": series[position]["close"] if position is not None else None,
                "exit_date": None, "exit_close": None, "outcome_available_at": None,
            }
            if position is not None and position + horizon < len(series):
                exit_row = series[position + horizon]
                item.update(exit_date=exit_row["trading_date"], exit_close=exit_row["close"],
                            outcome_available_at=exit_row["available_at"])
                item.update(sector_flow_outcome(feature["transition"], item["entry_close"], item["exit_close"]))
            elif position is None:
                item.update(status="unavailable", raw_return=None, excess_return=None, directional_return=None,
                            expected_direction=0)
            else:
                item.update(status="pending", raw_return=None, excess_return=None, directional_return=None,
                            expected_direction=0)
            provisional.append(item)
    medians: dict[tuple[date, int], float] = {}
    groups: dict[tuple[date, int], list[float]] = defaultdict(list)
    for item in provisional:
        if item["status"] == "matured" and item["raw_return"] is not None:
            groups[(item["trading_date"], item["horizon_days"])].append(float(item["raw_return"]))
    for key, values in groups.items():
        medians[key] = median(values)
    status_counts: dict[str, int] = {}
    for item in provisional:
        if item["status"] == "matured":
            benchmark = medians.get((item["trading_date"], item["horizon_days"]))
            evaluated = sector_flow_outcome(
                item["transition"], item["entry_close"], item["exit_close"],
                cross_section_median_return=benchmark,
            )
            item.update(evaluated)
        status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
    if provisional:
        with database.transaction() as connection:
            # One set-based upsert instead of one INSERT per (sector, day,
            # horizon) row; every field was already fully resolved above, so
            # the ON CONFLICT DO UPDATE mirrors the previous per-row statement
            # exactly, just batched via unnest.
            connection.execute(
                """INSERT INTO quant.sector_flow_daily_outcomes(
                       taxonomy_key,sector_key,signal_date,horizon_days,transition,status,
                       entry_date,exit_date,entry_close,exit_close,raw_return,cross_section_excess_return,
                       directional_return,outcome_available_at,quality_flags)
                   SELECT t.taxonomy_key,t.sector_key,t.signal_date,t.horizon_days,t.transition,t.status,
                          t.entry_date,t.exit_date,t.entry_close,t.exit_close,t.raw_return,t.excess_return,
                          t.directional_return,t.outcome_available_at,'[]'::jsonb
                     FROM unnest(%s::text[],%s::text[],%s::date[],%s::int[],%s::text[],%s::text[],
                                  %s::date[],%s::date[],%s::numeric[],%s::numeric[],%s::numeric[],%s::numeric[],
                                  %s::numeric[],%s::timestamptz[])
                          AS t(taxonomy_key,sector_key,signal_date,horizon_days,transition,status,
                               entry_date,exit_date,entry_close,exit_close,raw_return,excess_return,
                               directional_return,outcome_available_at)
                   ON CONFLICT(taxonomy_key,sector_key,signal_date,horizon_days) DO UPDATE SET
                     transition=EXCLUDED.transition,status=EXCLUDED.status,entry_date=EXCLUDED.entry_date,
                     exit_date=EXCLUDED.exit_date,entry_close=EXCLUDED.entry_close,exit_close=EXCLUDED.exit_close,
                     raw_return=EXCLUDED.raw_return,cross_section_excess_return=EXCLUDED.cross_section_excess_return,
                     directional_return=EXCLUDED.directional_return,
                     outcome_available_at=EXCLUDED.outcome_available_at,quality_flags=EXCLUDED.quality_flags,
                     updated_at=now()""",
                (
                    [item["taxonomy_key"] for item in provisional],
                    [item["sector_key"] for item in provisional],
                    [item["trading_date"] for item in provisional],
                    [item["horizon_days"] for item in provisional],
                    [item["transition"] for item in provisional],
                    [item["status"] for item in provisional],
                    [item["entry_date"] for item in provisional],
                    [item["exit_date"] for item in provisional],
                    [item["entry_close"] for item in provisional],
                    [item["exit_close"] for item in provisional],
                    [item.get("raw_return") for item in provisional],
                    [item.get("excess_return") for item in provisional],
                    [item.get("directional_return") for item in provisional],
                    [item["outcome_available_at"] for item in provisional],
                ),
            )
    return {"status": "completed", "as_of_date": str(as_of_date), "rows": len(provisional),
            "status_counts": status_counts, "horizons": list(horizons), "response_type": "close_to_later_close",
            "decision_eligible": False}


__all__ = ["rebuild_sector_flow_daily_features", "materialize_sector_flow_daily_outcomes"]
