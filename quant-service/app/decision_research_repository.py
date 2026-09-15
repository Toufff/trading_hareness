"""PostgreSQL boundary for terminal decision research and plan materialization."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from psycopg.types.json import Json

from .decision_research_contracts import DecisionResearchDossier
from .research_prices import adjusted_bars


def _hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def persist_dossier(connection: Any, dossier: DecisionResearchDossier) -> dict[str, Any]:
    payload = dossier.model_dump(mode="json")
    content_hash = _hash(payload)
    existing = connection.execute(
        "SELECT dossier_id,content_hash,status FROM quant.decision_research_dossiers WHERE dossier_key=%s",
        (dossier.dossier_key,),
    ).fetchone()
    if existing:
        if str(existing["content_hash"]) != content_hash:
            raise ValueError("dossier_key already exists with different terminal evidence")
        return {"status": "idempotent", "dossier_id": existing["dossier_id"], "research_status": existing["status"]}
    row = connection.execute(
        """INSERT INTO quant.decision_research_dossiers(
               dossier_key,as_of_date,symbol,name,strategy_family,model_version,status,conclusion,
               source_candidate_run_id,source_candidate_rank,evidence_snapshot,evidence_refs,content_hash)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING dossier_id,status""",
        (
            dossier.dossier_key, dossier.as_of_date, dossier.symbol, dossier.name,
            dossier.strategy_family, dossier.model_version, dossier.status, dossier.conclusion,
            dossier.source_candidate_run_id, dossier.source_candidate_rank,
            Json(payload["evidence_snapshot"]), Json(payload["evidence_refs"]), content_hash,
        ),
    ).fetchone()
    for gate, gate_payload in zip(dossier.gates, payload["gates"], strict=True):
        connection.execute(
            """INSERT INTO quant.decision_research_gates(
                   dossier_id,gate_key,label,verdict,independent_run,conclusion,evidence)
               VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (
                row["dossier_id"], gate.gate_key, gate.label, gate.verdict,
                gate.independent_run, gate.conclusion, Json(gate_payload["evidence"]),
            ),
        )
    return {"status": "created", "dossier_id": row["dossier_id"], "research_status": row["status"]}


def latest_exact_portfolio(connection: Any, account_key: str) -> dict[str, Any] | None:
    snapshot = connection.execute(
        """SELECT snapshot_id,observed_at,verification,cash,total_asset,total_market_value,source,metadata
             FROM quant.broker_portfolio_snapshots
            WHERE account_key=%s AND verification='verified_exact'
            ORDER BY observed_at DESC,recorded_at DESC LIMIT 1""",
        (account_key,),
    ).fetchone()
    if not snapshot:
        return None
    positions = connection.execute(
        """SELECT symbol,name,quantity,sellable_quantity,average_cost,market_price,market_value,
                  unrealized_pnl,position_weight_pct
             FROM quant.broker_position_snapshots WHERE snapshot_id=%s
            ORDER BY market_value DESC NULLS LAST,symbol""",
        (snapshot["snapshot_id"],),
    ).fetchall()
    return {**dict(snapshot), "positions": [dict(row) for row in positions]}


def _latest_lane_candidate_evidence(connection: Any, as_of_date: Any, limit: int) -> list[dict[str, Any]]:
    """Read the bounded research plan produced by the nine-lane scan.

    The former decision closure silently read ``post_close_strategy_candidates``
    from the legacy single scanner.  The reports, however, are produced from
    ``summary.strategy_lanes``.  Reading the persisted review plan here makes
    discovery, research and publication share one candidate identity.
    """
    rows = connection.execute(
        """WITH selected_run AS (
               SELECT run_id,summary->'strategy_lanes' AS lanes
                 FROM quant.post_close_strategy_runs
                WHERE as_of_date=%s AND status IN ('completed','partial')
                  AND summary ? 'strategy_lanes'
                ORDER BY updated_at DESC LIMIT 1
           ), planned AS (
               SELECT run_id,ordinality::int AS rank,plan,
                      plan->'memberships'->0 AS membership
                 FROM selected_run
                 CROSS JOIN LATERAL jsonb_array_elements(coalesce(lanes->'review_plan','[]'::jsonb))
                      WITH ORDINALITY item(plan,ordinality)
                ORDER BY ordinality LIMIT %s
           ), latest_basic AS (
               SELECT DISTINCT ON (row_data->>'ts_code')
                      row_data->>'ts_code' AS symbol,row_data,available_at
                 FROM quant.tushare_raw_records
                WHERE api_name='daily_basic' AND row_data->>'trade_date'=to_char(%s::date,'YYYYMMDD')
                ORDER BY row_data->>'ts_code',available_at DESC
           ), latest_flow AS (
               SELECT DISTINCT ON (symbol) symbol,net_amount,raw,available_at
                 FROM quant.stock_money_flow_daily
                WHERE trading_date=%s AND source='longhuvip_main_net'
                ORDER BY symbol,available_at DESC
           )
           SELECT p.run_id,p.rank,p.plan->>'symbol' AS symbol,p.plan->>'name' AS name,
                  'strategy_lane:'||(p.membership->>'lane') AS candidate_type,
                  (p.membership->>'rank_score')::numeric AS score,
                  jsonb_build_object('metrics',p.membership->'metrics') AS structure,
                  jsonb_build_object(
                    'sector_key',flow.raw->>'plate_id',
                    'label',coalesce(nullif(flow.raw#>>'{screen_snapshot,sector_label}',''),sector.item->>'label'),
                    'net_amount',sector.item->'net_inflow',
                    'flow_percentile',sector.item->'flow_percentile',
                    'exact_member_mapping',(flow.raw->>'plate_id' IS NOT NULL)
                  ) AS board_context,
                  '[]'::jsonb AS risk_flags,basic.row_data AS daily_basic,
                  basic.available_at AS basic_available_at,flow.net_amount AS main_net_amount,
                  flow.raw AS flow_raw,flow.available_at AS flow_available_at,
                  coalesce(bars.amount,nullif(p.membership#>>'{metrics,amount}','')::numeric) AS amount,
                  bars.close,bars.pre_close,bars.volume,bars.available_at AS bar_available_at
             FROM planned p
             LEFT JOIN latest_basic basic ON basic.symbol=p.plan->>'symbol'
             LEFT JOIN latest_flow flow ON flow.symbol=p.plan->>'symbol'
             LEFT JOIN quant.canonical_bars_daily bars
                    ON bars.symbol=p.plan->>'symbol' AND bars.trading_date=%s
             LEFT JOIN LATERAL (
                    SELECT item
                      FROM quant.intraday_board_reports report
                      CROSS JOIN LATERAL jsonb_array_elements(coalesce(report.payload->'items','[]'::jsonb)) item
                     WHERE (report.observed_at AT TIME ZONE 'Asia/Shanghai')::date=%s
                       AND item->>'sector_key'=flow.raw->>'plate_id'
                     ORDER BY report.observed_at DESC LIMIT 1
             ) sector ON true
            ORDER BY p.rank""",
        (as_of_date, limit, as_of_date, as_of_date, as_of_date, as_of_date),
    ).fetchall()
    return [dict(row) for row in rows]


def _latest_legacy_candidate_evidence(connection: Any, as_of_date: Any, limit: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """WITH selected_run AS (
               SELECT run_id FROM quant.post_close_strategy_runs
                WHERE as_of_date=%s AND status IN ('completed','partial')
                ORDER BY updated_at DESC LIMIT 1
           ), latest_basic AS (
               SELECT DISTINCT ON (row_data->>'ts_code')
                      row_data->>'ts_code' AS symbol,row_data,available_at
                 FROM quant.tushare_raw_records
                WHERE api_name='daily_basic' AND row_data->>'trade_date'=to_char(%s::date,'YYYYMMDD')
                ORDER BY row_data->>'ts_code',available_at DESC
           ), latest_flow AS (
               SELECT DISTINCT ON (symbol) symbol,net_amount,raw,available_at
                 FROM quant.stock_money_flow_daily
                WHERE trading_date=%s AND source='longhuvip_main_net'
                ORDER BY symbol,available_at DESC
           )
           SELECT c.run_id,c.rank,c.symbol,i.name,c.candidate_type,c.score,c.structure,c.board_context,
                  c.risk_flags,b.row_data AS daily_basic,b.available_at AS basic_available_at,
                  f.net_amount AS main_net_amount,f.raw AS flow_raw,f.available_at AS flow_available_at,
                  d.amount,d.close,d.pre_close,d.volume,d.available_at AS bar_available_at
             FROM selected_run r
             JOIN quant.post_close_strategy_candidates c ON c.run_id=r.run_id
             LEFT JOIN quant.instruments i ON i.symbol=c.symbol
             LEFT JOIN latest_basic b ON b.symbol=c.symbol
             LEFT JOIN latest_flow f ON f.symbol=c.symbol
             LEFT JOIN quant.canonical_bars_daily d ON d.symbol=c.symbol AND d.trading_date=%s
            ORDER BY c.rank LIMIT %s""",
        (as_of_date, as_of_date, as_of_date, as_of_date, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def latest_candidate_evidence(connection: Any, as_of_date: Any, limit: int) -> list[dict[str, Any]]:
    """Prefer the nine-lane research plan; retain legacy fallback for old dates."""
    planned = _latest_lane_candidate_evidence(connection, as_of_date, limit)
    if planned:
        return planned
    lane_run = connection.execute(
        """SELECT 1 FROM quant.post_close_strategy_runs
            WHERE as_of_date=%s AND status IN ('completed','partial')
              AND summary ? 'strategy_lanes'
            ORDER BY updated_at DESC LIMIT 1""",
        (as_of_date,),
    ).fetchone()
    # An empty current lane plan means "no prioritized research candidate",
    # not permission to resurrect a different legacy scanner's symbols.
    return [] if lane_run else _latest_legacy_candidate_evidence(connection, as_of_date, limit)


def holding_evidence(connection: Any, as_of_date: Any, symbol: str) -> dict[str, Any] | None:
    row = connection.execute(
        """WITH latest_basic AS (
               SELECT row_data,available_at FROM quant.tushare_raw_records
                WHERE api_name='daily_basic' AND row_data->>'ts_code'=%s
                  AND row_data->>'trade_date'=to_char(%s::date,'YYYYMMDD')
                ORDER BY available_at DESC LIMIT 1
           ), latest_flow AS (
               SELECT net_amount,raw,available_at FROM quant.stock_money_flow_daily
                WHERE symbol=%s AND trading_date=%s AND source='longhuvip_main_net'
                ORDER BY available_at DESC LIMIT 1
           ), latest_board AS (
               SELECT item FROM quant.intraday_board_reports report
               CROSS JOIN LATERAL jsonb_array_elements(coalesce(report.payload->'items','[]'::jsonb)) item
                WHERE report.status='completed'
                  AND (report.observed_at AT TIME ZONE 'Asia/Shanghai')::date=%s
                  AND report.source_status->>'provider'='longhuvip_composite'
                  AND item->>'sector_key'=(SELECT raw->>'plate_id' FROM latest_flow)
                ORDER BY report.observed_at DESC LIMIT 1
           )
           SELECT i.name,b.row_data AS daily_basic,b.available_at AS basic_available_at,
                  f.net_amount AS main_net_amount,f.raw AS flow_raw,f.available_at AS flow_available_at,
                  board.item AS board_context,d.amount,d.close,d.pre_close,d.volume,d.available_at AS bar_available_at
             FROM quant.instruments i
             LEFT JOIN latest_basic b ON true LEFT JOIN latest_flow f ON true LEFT JOIN latest_board board ON true
             LEFT JOIN quant.canonical_bars_daily d ON d.symbol=i.symbol AND d.trading_date=%s
            WHERE i.symbol=%s""",
        (symbol, as_of_date, symbol, as_of_date, as_of_date, as_of_date, symbol),
    ).fetchone()
    if not row:
        return None
    # canonical_bars_daily carries the licensed, incremental adj_factor
    # series; stock_brain_tencent_qfq's rows used a hardcoded adj_factor=1
    # and were never validated the same way.  adjusted_bars() fails closed
    # (returns no bars) rather than silently mixing an incomplete factor
    # window with raw prices across a corporate action.
    raw_bars = [
        dict(item) for item in connection.execute(
            """SELECT trading_date,open,high,low,close,volume,adj_factor
                 FROM quant.canonical_bars_daily
                WHERE symbol=%s AND trading_date<=%s
                ORDER BY trading_date DESC LIMIT 30""",
            (symbol, as_of_date),
        ).fetchall()
    ]
    research_bars, _quality_flags = adjusted_bars(raw_bars)
    bars = [
        {
            "trading_date": item["trading_date"], "volume": item.get("volume"),
            "open": item.get("research_open"), "high": item.get("research_high"),
            "low": item.get("research_low"), "close": item.get("research_close"),
        }
        for item in (research_bars or [])
    ]
    legacy = connection.execute(
        """SELECT payload FROM quant.legacy_source_records
            WHERE source_system='stock-brain' AND source_table='research_runs'
              AND payload->>'security_code'=split_part(%s,'.',1)
              AND payload->>'status' IN ('passed','rejected')
            ORDER BY coalesce(payload->>'finished_at',payload->>'started_at') DESC LIMIT 1""",
        (symbol,),
    ).fetchone()
    return {**dict(row), "bars": [dict(item) for item in reversed(bars)],
            "legacy_terminal_research": dict(legacy["payload"]) if legacy else None}


def latest_dossiers(connection: Any, as_of_date: Any, limit: int = 100) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT d.dossier_id,d.dossier_key,d.as_of_date,d.symbol,d.name,d.strategy_family,
                  d.model_version,d.status,d.conclusion,d.source_candidate_rank,d.evidence_snapshot,
                  d.evidence_refs,d.created_at,
                  coalesce(jsonb_agg(jsonb_build_object(
                    'gate_key',g.gate_key,'label',g.label,'verdict',g.verdict,
                    'independent_run',g.independent_run,'conclusion',g.conclusion,'evidence',g.evidence
                  ) ORDER BY g.gate_key) FILTER (WHERE g.gate_key IS NOT NULL),'[]'::jsonb) AS gates
             FROM quant.decision_research_dossiers d
             LEFT JOIN quant.decision_research_gates g ON g.dossier_id=d.dossier_id
            WHERE d.as_of_date=%s
            GROUP BY d.dossier_id ORDER BY d.status='passed' DESC,d.source_candidate_rank NULLS LAST,d.symbol
            LIMIT %s""",
        (as_of_date, limit),
    ).fetchall()
    return [dict(row) for row in rows]


__all__ = [
    "holding_evidence", "latest_candidate_evidence", "latest_dossiers",
    "latest_exact_portfolio", "persist_dossier",
]
