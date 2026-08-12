"""Bounded read-only projections for persisted sector, concept, and member evidence."""

from __future__ import annotations

from datetime import date
from typing import Any


def concept_member_backfill_status(
    database: Any, trade_date: date | None, *, automatic_enabled: bool, batch_size: int,
) -> dict[str, Any]:
    with database.transaction() as connection:
        selected_date = trade_date or connection.execute(
            "SELECT max(trading_date) latest FROM quant.sector_market_observations WHERE taxonomy_key='ths_concept_flow'"
        ).fetchone()["latest"]
        if selected_date is None:
            return {"trade_date": None, "total_concepts": 0, "mapped_concepts": 0, "states": [], "notice": "尚未同步同花顺概念资金流。"}
        total = connection.execute(
            "SELECT count(*)::int total FROM quant.sector_market_observations WHERE taxonomy_key='ths_concept_flow' AND trading_date=%s",
            (selected_date,),
        ).fetchone()["total"]
        mapped = connection.execute(
            """SELECT count(*)::int total FROM quant.sector_member_sync_state
                WHERE taxonomy_key='ths_concept_flow' AND trading_date=%s AND state IN ('completed','empty')""",
            (selected_date,),
        ).fetchone()["total"]
        states = connection.execute(
            """SELECT sync.state,count(*)::int boards,
                      coalesce(sum(active.member_count),0)::int members,
                      sum(sync.member_count)::int evidence_rows,
                      max(sync.updated_at) latest_updated_at
                 FROM quant.sector_member_sync_state sync
                 LEFT JOIN LATERAL (
                     SELECT count(*)::int member_count FROM quant.sector_membership_history history
                      WHERE history.taxonomy_key=sync.taxonomy_key AND history.sector_key=sync.sector_key
                        AND history.effective_to IS NULL
                 ) active ON true
                WHERE sync.taxonomy_key='ths_concept_flow' AND sync.trading_date=%s
                GROUP BY sync.state ORDER BY sync.state""",
            (selected_date,),
        ).fetchall()
    return {
        "trade_date": str(selected_date), "total_concepts": total, "mapped_concepts": mapped,
        "complete": int(mapped) == int(total), "states": states,
        "automatic": {"enabled": automatic_enabled, "batch_size": batch_size},
        "notice": "只有精确成员已完成的概念才可展示完整 Top10；members 是当前有效成员，evidence_rows 是包含历史成员的原始证据行。",
    }


def concept_sector_signals(database: Any, trade_date: date | None, limit: int) -> dict[str, Any]:
    """Return a transparent persisted THS concept scan without provider calls."""
    with database.transaction() as connection:
        selected_date = trade_date or connection.execute(
            "SELECT max(trading_date) latest FROM quant.sector_market_observations WHERE taxonomy_key='ths_concept_flow'"
        ).fetchone()["latest"]
        if selected_date is None:
            return {"trade_date": None, "items": [], "scoring": {"decision_eligible": False}}
        rows = connection.execute(
            """WITH concept AS (
                   SELECT o.sector_key,s.label,o.close,o.change_pct,o.net_amount,o.net_buy_amount,o.net_sell_amount,
                          o.constituent_count,o.leading_label,o.provider_key,o.available_at,o.raw,
                          percent_rank() OVER (ORDER BY o.net_amount NULLS FIRST) AS flow_percentile
                     FROM quant.sector_market_observations o
                     JOIN quant.sectors s ON s.taxonomy_key=o.taxonomy_key AND s.sector_key=o.sector_key
                    WHERE o.taxonomy_key='ths_concept_flow' AND o.trading_date=%s
                 )
                 SELECT c.*,ls.provider_key strength_provider,ls.raw strength_raw,
                        nullif(ls.raw->>'up_nums','')::numeric up_nums,
                        nullif(ls.raw->>'cons_nums','')::numeric strength_constituents,
                        nullif(ls.raw->>'days','')::numeric streak_days
                   FROM concept c
              LEFT JOIN quant.sector_market_observations ls
                     ON ls.taxonomy_key='ths_limit_strength' AND ls.trading_date=%s AND ls.sector_key=c.sector_key
                  ORDER BY c.net_amount DESC NULLS LAST,c.label LIMIT %s""",
            (selected_date, selected_date, max(1, min(limit, 1000))),
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        flow_score = round(float(item["flow_percentile"] or 0) * 100, 2)
        momentum_score = round(max(0.0, min(100.0, 50.0 + float(item["change_pct"] or 0) * 5.0)), 2)
        if item["up_nums"] is None:
            strength_score = None
            aggregate_score = round(flow_score * 0.65 + momentum_score * 0.35, 2)
        else:
            strength_score = round(min(100.0, float(item["up_nums"]) * 10.0 + float(item["streak_days"] or 0) * 3.0), 2)
            aggregate_score = round(flow_score * 0.50 + momentum_score * 0.30 + strength_score * 0.20, 2)
        item.update({"flow_score": flow_score, "momentum_score": momentum_score,
                     "strength_score": strength_score, "aggregate_score": aggregate_score})
        item.pop("raw", None)
        item.pop("strength_raw", None)
        items.append(item)
    items.sort(key=lambda item: float(item["aggregate_score"]), reverse=True)
    return {
        "trade_date": str(selected_date), "items": items,
        "scoring": {"decision_eligible": False, "purpose": "board_scan_only",
                    "weights": "flow 65% + momentum 35%; when limit-up strength exists: 50% + 30% + 20%"},
    }


def concept_limit_candidates(database: Any, trade_date: date | None, limit: int) -> dict[str, Any]:
    with database.transaction() as connection:
        selected_date = trade_date or connection.execute(
            "SELECT max(trading_date) latest FROM quant.sector_limit_candidates WHERE taxonomy_key='ths_concept_flow'"
        ).fetchone()["latest"]
        if selected_date is None:
            return {"trade_date": None, "items": [], "decision_eligible": False}
        rows = connection.execute(
            """SELECT c.sector_key,s.label concept_label,c.symbol,c.name,c.limit_tag,c.limit_type,c.pct_change,c.price,c.limit_amount,
                      c.turnover_rate,c.open_num,c.status,c.description,c.provider_key,c.available_at,
                      coalesce(c.raw->>'membership_fetch_status','unknown') membership_status,
                      flow.net_amount board_net_amount,flow.change_pct board_change_pct,flow.leading_label board_leading_label
                 FROM quant.sector_limit_candidates c
                 JOIN quant.sectors s ON s.taxonomy_key=c.taxonomy_key AND s.sector_key=c.sector_key
            LEFT JOIN quant.sector_market_observations flow ON flow.taxonomy_key='ths_concept_flow' AND flow.sector_key=c.sector_key
                      AND flow.trading_date=c.trading_date
                WHERE c.taxonomy_key='ths_concept_flow' AND c.trading_date=%s
                ORDER BY flow.net_amount DESC NULLS LAST,c.limit_amount DESC NULLS LAST,c.symbol LIMIT %s""",
            (selected_date, max(1, min(limit, 200))),
        ).fetchall()
    return {"trade_date": str(selected_date), "items": rows, "decision_eligible": False,
            "matching_rule": "same-day THS concept member code equals THS limit-up-pool stock code"}


def sector_flows(database: Any, taxonomy_key: str, trade_date: date | None, limit: int) -> dict[str, Any]:
    with database.transaction() as connection:
        selected_date = trade_date or connection.execute(
            "SELECT max(trading_date) latest FROM quant.sector_market_observations WHERE taxonomy_key=%s", (taxonomy_key,)
        ).fetchone()["latest"]
        if selected_date is None:
            return {"taxonomy_key": taxonomy_key, "trade_date": None, "items": []}
        rows = connection.execute(
            """SELECT o.taxonomy_key,o.sector_key,s.label,o.trading_date,o.close,o.change_pct,o.net_amount,o.net_buy_amount,o.net_sell_amount,
                      o.constituent_count,o.leading_symbol,o.leading_label,o.provider_key,o.available_at
                 FROM quant.sector_market_observations o JOIN quant.sectors s ON s.taxonomy_key=o.taxonomy_key AND s.sector_key=o.sector_key
                WHERE o.taxonomy_key=%s AND o.trading_date=%s
                ORDER BY o.net_amount DESC NULLS LAST,s.label LIMIT %s""",
            (taxonomy_key, selected_date, max(1, min(limit, 500))),
        ).fetchall()
    return {"taxonomy_key": taxonomy_key, "trade_date": str(selected_date), "items": rows}


def market_sectors(database: Any, taxonomy_key: str, limit: int, offset: int) -> dict[str, Any]:
    bounded_limit, bounded_offset = max(1, min(limit, 1000)), max(0, offset)
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT s.taxonomy_key,s.sector_key,s.label,s.metadata,s.updated_at,count(m.symbol)::int active_members
                 FROM quant.sectors s LEFT JOIN quant.sector_membership_history m
                   ON m.taxonomy_key=s.taxonomy_key AND m.sector_key=s.sector_key AND m.effective_to IS NULL
                WHERE s.taxonomy_key=%s GROUP BY s.taxonomy_key,s.sector_key,s.label,s.metadata,s.updated_at
                ORDER BY s.label LIMIT %s OFFSET %s""",
            (taxonomy_key, bounded_limit, bounded_offset),
        ).fetchall()
        total = connection.execute("SELECT count(*)::int total FROM quant.sectors WHERE taxonomy_key=%s", (taxonomy_key,)).fetchone()["total"]
    return {"taxonomy_key": taxonomy_key, "items": rows, "limit": bounded_limit, "offset": bounded_offset, "total": total,
            "next_offset": bounded_offset + len(rows) if bounded_offset + len(rows) < total else None}


def sector_members(database: Any, sector_key: str, taxonomy_key: str, limit: int, offset: int) -> dict[str, Any]:
    bounded_limit, bounded_offset = max(1, min(limit, 1000)), max(0, offset)
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT m.symbol,i.name,i.industry,m.effective_from,m.effective_to,m.provider_key,m.available_at
                 FROM quant.sector_membership_history m JOIN quant.instruments i ON i.symbol=m.symbol
                WHERE m.taxonomy_key=%s AND m.sector_key=%s AND m.effective_to IS NULL
                ORDER BY m.symbol LIMIT %s OFFSET %s""",
            (taxonomy_key, sector_key, bounded_limit, bounded_offset),
        ).fetchall()
        total = connection.execute(
            "SELECT count(*)::int total FROM quant.sector_membership_history WHERE taxonomy_key=%s AND sector_key=%s AND effective_to IS NULL",
            (taxonomy_key, sector_key),
        ).fetchone()["total"]
    return {"taxonomy_key": taxonomy_key, "sector_key": sector_key, "items": rows,
            "limit": bounded_limit, "offset": bounded_offset, "total": total,
            "next_offset": bounded_offset + len(rows) if bounded_offset + len(rows) < total else None}


__all__ = [
    "concept_limit_candidates", "concept_member_backfill_status", "concept_sector_signals",
    "market_sectors", "sector_flows", "sector_members",
]
