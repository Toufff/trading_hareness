"""Native-async evidence projection for one stock research workbench."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any


async def _all(connection: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    result = await connection.execute(sql, params)
    return [dict(row) for row in await result.fetchall()]


async def _one(connection: Any, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    result = await connection.execute(sql, params)
    row = await result.fetchone()
    return dict(row) if row else None


async def stock_workbench_evidence(
    async_database: Any,
    symbol: str,
    as_of_date: date,
    *,
    lookback_days: int,
    knowledge_cutoff: datetime | None = None,
) -> dict[str, Any]:
    """Read bounded evidence; provider I/O is deliberately outside this repository."""
    lower_date = as_of_date - timedelta(days=max(60, lookback_days * 2))
    now = datetime.now(timezone.utc)
    event_boundary = knowledge_cutoff or now
    async with async_database.transaction() as connection:
        instrument = await _one(
            connection,
            "SELECT symbol,name,industry,exchange,is_st,source,updated_at FROM quant.instruments WHERE symbol=%s",
            (symbol,),
        )
        flows = await _all(
            connection,
            """SELECT trading_date,source,provider,net_amount,net_amount_rate,buy_elg_amount,buy_lg_amount,
                      buy_md_amount,buy_sm_amount,available_at,raw
                 FROM quant.stock_money_flow_daily
                WHERE symbol=%s AND trading_date<=%s AND trading_date>=%s
                  AND source='longhuvip_main_net' AND provider='longhuvip_composite'
                ORDER BY trading_date DESC LIMIT %s""",
            (symbol, as_of_date, lower_date, min(300, lookback_days)),
        )
        fundamentals = await _all(
            connection,
            """SELECT trading_date,turnover_rate,volume_ratio,pe,pb,total_mv,circ_mv,provider,available_at
                 FROM quant.daily_fundamentals
                WHERE symbol=%s AND trading_date<=%s AND trading_date>=%s
                  AND provider='longhuvip_composite'
                ORDER BY trading_date DESC LIMIT %s""",
            (symbol, as_of_date, lower_date, min(300, lookback_days)),
        )
        events = await _all(
            connection,
            """SELECT event_id,event_type,occurred_at,available_at,source,title,body,url,availability_basis
                 FROM quant.market_events
                WHERE symbol=%s AND available_at<%s
                ORDER BY available_at DESC LIMIT 80""",
            (symbol, event_boundary),
        )
        plan = await _one(
            connection,
            """SELECT plan_key,plan_kind,symbol,name,as_of_at,valid_until,action,entry_zone,add_trigger,
                      reduce_trigger,exit_trigger,stop_price,target_prices,max_position_pct,rationale,
                      evidence_refs,risk_flags,metadata
                 FROM quant.personal_trade_plans
                WHERE symbol=%s AND as_of_at<=%s AND valid_until>=%s
                ORDER BY created_at DESC,as_of_at DESC LIMIT 1""",
            (symbol, now, now),
        )
        sector_rows = await _all(
            connection,
            """WITH current_membership AS (
                   SELECT DISTINCT ON (membership.taxonomy_key,membership.sector_key)
                          membership.taxonomy_key,membership.sector_key,sector.label
                     FROM quant.sector_membership_history membership
                     JOIN quant.sectors sector ON sector.taxonomy_key=membership.taxonomy_key
                                              AND sector.sector_key=membership.sector_key
                    WHERE membership.symbol=%s AND membership.effective_from<=%s
                      AND (membership.effective_to IS NULL OR membership.effective_to>=%s)
                      AND membership.known_at<=%s
                    ORDER BY membership.taxonomy_key,membership.sector_key,membership.known_at DESC
               )
               SELECT member.taxonomy_key,member.sector_key,member.label,feature.trading_date,
                      feature.provider_key,feature.status,feature.transition,feature.net_amount,
                      feature.net_acceleration,feature.rank_percentile,feature.flow_sign_streak,
                      feature.change_pct,feature.price_flow_divergence,feature.limit_up_count,
                      feature.features,feature.quality_flags,feature.available_at
                 FROM current_membership member
                 LEFT JOIN LATERAL (
                   SELECT * FROM quant.sector_flow_daily_features feature
                    WHERE feature.taxonomy_key=member.taxonomy_key AND feature.sector_key=member.sector_key
                      AND feature.trading_date<=%s
                    ORDER BY feature.trading_date DESC LIMIT 1
                 ) feature ON true
                ORDER BY feature.rank_percentile DESC NULLS LAST,member.taxonomy_key,member.sector_key
                LIMIT 24""",
            (symbol, as_of_date, as_of_date, event_boundary, as_of_date),
        )
        if not sector_rows:
            # Longhu's daily stock-flow row carries the vendor industry id for
            # every symbol.  Older deployments persisted that evidence but
            # failed to materialise sector_membership_history.  Keep the
            # workbench useful while remaining explicit that this is a
            # same-row, point-in-time projection rather than timeless master
            # data.  The matching board item supplies breadth/flow context
            # when the close board report is present.
            derived_sector = await _one(
                connection,
                """SELECT 'longhu_ths_industry'::text AS taxonomy_key,
                          flow.raw->>'plate_id' AS sector_key,
                          coalesce(nullif(flow.raw#>>'{screen_snapshot,sector_label}',''),
                                   board.item->>'label') AS label,
                          flow.trading_date,'longhuvip_composite'::text AS provider_key,
                          'ready'::text AS status,NULL::text AS transition,
                          nullif(board.item->>'net_inflow','')::numeric AS net_amount,
                          NULL::numeric AS net_acceleration,NULL::numeric AS rank_percentile,
                          NULL::integer AS flow_sign_streak,
                          nullif(board.item->>'change_pct','')::numeric AS change_pct,
                          NULL::text AS price_flow_divergence,
                          nullif(board.item->>'limit_up_count','')::integer AS limit_up_count,
                          coalesce(board.item,'{}'::jsonb) AS features,
                          jsonb_build_array('derived_from_longhu_stock_flow_plate_id') AS quality_flags,
                          flow.available_at
                     FROM quant.stock_money_flow_daily flow
                     LEFT JOIN LATERAL (
                       SELECT item
                         FROM quant.intraday_board_reports report
                         CROSS JOIN LATERAL jsonb_array_elements(coalesce(report.payload->'items','[]'::jsonb)) item
                        WHERE (report.observed_at AT TIME ZONE 'Asia/Shanghai')::date=flow.trading_date
                          AND item->>'sector_key'=flow.raw->>'plate_id'
                        ORDER BY report.observed_at DESC LIMIT 1
                     ) board ON true
                    WHERE flow.symbol=%s AND flow.trading_date<=%s
                      AND flow.source='longhuvip_main_net' AND flow.provider='longhuvip_composite'
                      AND nullif(flow.raw->>'plate_id','') IS NOT NULL
                    ORDER BY flow.trading_date DESC LIMIT 1""",
                (symbol, as_of_date),
            )
            if derived_sector:
                sector_rows = [derived_sector]
        market_rows = await _all(
            connection,
            """SELECT trading_date,stock_count,advancers,decliners,unchanged,median_change_pct,
                      mean_change_pct,total_amount_kcny,source_provider,available_at,quality_flags
                 FROM quant.daily_market_aggregates
                WHERE trading_date<=%s AND trading_date>=%s
                ORDER BY trading_date DESC LIMIT 30""",
            (as_of_date, lower_date),
        )
        if not market_rows:
            # Full-market close snapshots are the authoritative daily output
            # of the production pipeline.  daily_market_aggregates is an
            # optional historical/materialised table and can legitimately lag
            # behind it; never turn that lag into an empty market panel.
            snapshot = await _one(
                connection,
                """SELECT exchange_date,universe_count,observed_at,summary,quality_flags
                     FROM quant.market_snapshot_runs
                    WHERE session='close' AND status='ready' AND exchange_date<=%s
                    ORDER BY exchange_date DESC,observed_at DESC LIMIT 1""",
                (as_of_date,),
            )
            if snapshot:
                summary = snapshot.get("summary") or {}
                amount = summary.get("market_amount")
                market_rows = [{
                    "trading_date": snapshot.get("exchange_date"),
                    "stock_count": snapshot.get("universe_count") or summary.get("quoted_symbols"),
                    "advancers": summary.get("advancers"),
                    "decliners": summary.get("decliners"),
                    "unchanged": summary.get("unchanged"),
                    "median_change_pct": summary.get("median_change_pct"),
                    "mean_change_pct": summary.get("mean_change_pct"),
                    "total_amount_kcny": (float(amount) / 1000.0) if amount is not None else None,
                    "source_provider": "market_snapshot_runs",
                    "available_at": snapshot.get("observed_at"),
                    "quality_flags": snapshot.get("quality_flags") or [],
                }]
        regime = await _one(
            connection,
            """SELECT trading_date,model_version,regime_label,evidence,calculated_at
                 FROM quant.market_regime_daily WHERE trading_date<=%s
                ORDER BY trading_date DESC LIMIT 1""",
            (as_of_date,),
        )
        sentiment = await _one(
            connection,
            """SELECT trading_date,model_version,stage,sealed_count,broken_count,broken_rate,max_board_height,
                      high_board_count,promotion_rate,prior_limit_up_premium_pct,evidence,calculated_at
                 FROM quant.sentiment_cycle_daily WHERE trading_date<=%s
                ORDER BY trading_date DESC LIMIT 1""",
            (as_of_date,),
        )
    return {
        "instrument": instrument,
        "flows": list(reversed(flows)),
        "fundamentals": list(reversed(fundamentals)),
        "events": list(reversed(events)),
        "active_plan": plan,
        "sectors": sector_rows,
        "market": list(reversed(market_rows)),
        "regime": regime,
        "sentiment": sentiment,
    }


__all__ = ["stock_workbench_evidence"]
