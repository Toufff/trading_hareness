"""Materialize daily THS sector-flow migration with exact-member LHB context."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from psycopg.types.json import Json

from .sector_flow_features import sector_flow_feature


def _sign(value: Any) -> int:
    number = float(value or 0)
    return 1 if number > 0 else -1 if number < 0 else 0


def rebuild_sector_flow_daily_features(database: Any, start_date: date, end_date: date) -> dict[str, Any]:
    if end_date < start_date or (end_date - start_date).days > 45:
        raise ValueError("sector-flow feature rebuild requires an ordered range capped at 45 days")
    context_start = start_date - timedelta(days=10)
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
            """WITH lhb_candidates AS (
                   SELECT (occurred_at AT TIME ZONE 'Asia/Shanghai')::date AS day,symbol,
                          COALESCE(NULLIF(body::jsonb->>'龙虎榜净买额','')::numeric,0) AS net_amount,
                          COALESCE(NULLIF(body::jsonb->>'龙虎榜成交额','')::numeric,0) AS lhb_amount,
                          1 AS source_priority,available_at
                     FROM quant.market_events WHERE event_type='lhb_event'
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
                      AND m.effective_from<=l.day AND (m.effective_to IS NULL OR m.effective_to>=l.day)
                    GROUP BY l.day,m.sector_key
               ), limits AS (
                   SELECT day,m.sector_key,count(DISTINCT event.symbol) AS limit_up_count
                     FROM (
                         SELECT symbol,(occurred_at AT TIME ZONE 'Asia/Shanghai')::date AS day
                           FROM quant.market_events WHERE event_type='limit_up_pool'
                     ) event
                     JOIN quant.sector_membership_history m
                       ON m.taxonomy_key='ths_concept_flow' AND m.symbol=event.symbol
                      AND m.effective_from<=event.day AND (m.effective_to IS NULL OR m.effective_to>=event.day)
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
    with database.transaction() as connection:
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
                quality_flags = []
                if index == 0:
                    quality_flags.append("previous_flow_missing")
                connection.execute(
                    """INSERT INTO quant.sector_flow_daily_features(
                           taxonomy_key,sector_key,trading_date,provider_key,available_at,status,transition,
                           net_amount,previous_net_amount,net_change_amount,net_acceleration,rank_percentile,
                           flow_sign_streak,change_pct,price_flow_divergence,lhb_stock_count,lhb_net_amount,
                           lhb_negative_count,lhb_sell_pressure_ratio,limit_up_count,features,quality_flags)
                       VALUES('ths_concept_flow',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                    (
                        sector_key,row["trading_date"],row["provider_key"],row["available_at"],
                        "partial" if quality_flags else "ready",feature["transition"],feature["net_amount"],
                        feature["previous_net_amount"],feature["net_change_amount"],feature["net_acceleration"],
                        feature["rank_percentile"],feature["flow_sign_streak"],row["change_pct"],
                        feature["price_flow_divergence"],feature["lhb_stock_count"],feature["lhb_net_amount"],
                        feature["lhb_negative_count"],feature["lhb_sell_pressure_ratio"],feature["limit_up_count"],
                        Json(feature),Json(quality_flags),
                    ),
                )
                stored += 1
                transitions[feature["transition"]] = transitions.get(feature["transition"], 0) + 1
    return {
        "status": "completed", "start_date": str(start_date), "end_date": str(end_date),
        "stored": stored, "transition_counts": transitions, "source": "stored_evidence_only",
        "provider_calls": 0, "decision_eligible": False,
    }


__all__ = ["rebuild_sector_flow_daily_features"]
