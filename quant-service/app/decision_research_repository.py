"""PostgreSQL boundary for terminal decision research and plan materialization."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from psycopg.types.json import Json

from .decision_research_contracts import DecisionResearchDossier


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
        """SELECT snapshot_id,observed_at,verification,cash,total_asset,total_market_value
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


def latest_candidate_evidence(connection: Any, as_of_date: Any, limit: int) -> list[dict[str, Any]]:
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
    bars = connection.execute(
        """SELECT trading_date,open,high,low,close,volume
             FROM quant.research_adjusted_bars_daily
            WHERE symbol=%s AND adjustment_basis='qfq' AND trading_date<=%s
            ORDER BY trading_date DESC LIMIT 30""",
        (symbol, as_of_date),
    ).fetchall()
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
