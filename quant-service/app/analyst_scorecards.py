"""Local-only analyst scorecard materialization."""

from __future__ import annotations

from datetime import date
from typing import Any, Callable


def readiness(connection: Any) -> list[dict[str, Any]]:
    """Explain the local-only maturity gate for each analyst scorecard."""
    rows = connection.execute(
        """SELECT a.remote_analyst_id,a.name,
                  count(DISTINCT c.claim_id)::int stock_claims,
                  count(DISTINCT c.claim_id) FILTER (WHERE c.direction<>0)::int directional_stock_claims,
                  count(DISTINCT c.claim_id) FILTER (WHERE c.direction=0)::int neutral_stock_claims,
                  count(DISTINCT o.outcome_id)::int settled_stock_outcomes,
                  max(c.available_at) latest_claim_at
             FROM quant.remote_analysts a
             LEFT JOIN quant.analyst_claims c ON c.remote_analyst_id=a.remote_analyst_id AND c.scope='stock'
             LEFT JOIN quant.outcomes o ON o.claim_id=c.claim_id
             GROUP BY a.remote_analyst_id,a.name
             ORDER BY a.name,a.remote_analyst_id"""
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        directional = int(item["directional_stock_claims"] or 0)
        settled = int(item["settled_stock_outcomes"] or 0)
        reason = (
            "no_directional_stock_claims" if directional == 0 else
            "fewer_than_30_settled_stock_outcomes" if settled < 30 else
            "eligible_for_scorecard_review"
        )
        result.append({**item, "mature": settled >= 30, "reason": reason})
    return result


def recompute(
    as_of_date: date | None = None,
    *,
    cn_today: Callable[[], date],
    db: Any,
    readiness: Callable[[Any], list[dict[str, Any]]],
) -> dict[str, Any]:
    as_of_date = as_of_date or cn_today()
    methodology = "excess-return-v1"
    with db.transaction() as connection:
        rows = connection.execute(
            r"""WITH signal_source AS (
                -- Versioned remote archive claims are the sole analyst evidence
                -- input.  The retired analyst_signals table is intentionally
                -- excluded so an old local extraction cannot affect a scorecard.
                SELECT remote_analyst_id AS analyst_id,subject_key AS symbol,direction,strength,horizon_days,available_at
                  FROM quant.analyst_claims
                 WHERE scope='stock' AND subject_key ~ '^\d{6}\.(SH|SZ|BJ)$'
              ), entry_exit AS (
                SELECT s.analyst_id,s.horizon_days,s.direction,s.strength,
                  (SELECT b.trading_date FROM quant.canonical_bars_daily b
                    WHERE b.symbol=s.symbol
                      AND b.trading_date > (s.available_at AT TIME ZONE 'Asia/Shanghai')::date
                      AND b.trading_date <= %s
                    ORDER BY b.trading_date LIMIT 1) AS entry_date,
                  s.symbol
                FROM signal_source s
                WHERE (s.available_at AT TIME ZONE 'Asia/Shanghai')::date <= %s
                  AND s.direction <> 0
              ), priced AS (
                SELECT e.*, be.close AS entry_close,
                  (SELECT bx.close FROM quant.canonical_bars_daily bx WHERE bx.symbol=e.symbol AND bx.trading_date >= e.entry_date
                    AND bx.trading_date <= %s
                    ORDER BY bx.trading_date OFFSET (e.horizon_days - 1) LIMIT 1) AS exit_close,
                  (SELECT bx.trading_date FROM quant.canonical_bars_daily bx WHERE bx.symbol=e.symbol AND bx.trading_date >= e.entry_date
                    AND bx.trading_date <= %s
                    ORDER BY bx.trading_date OFFSET (e.horizon_days - 1) LIMIT 1) AS exit_date,
                  benchmark_entry.close AS benchmark_entry_close, benchmark_exit.close AS benchmark_exit_close
                FROM entry_exit e
                LEFT JOIN quant.canonical_bars_daily be ON be.symbol=e.symbol AND be.trading_date=e.entry_date
                LEFT JOIN quant.canonical_bars_daily benchmark_entry ON benchmark_entry.symbol='000300.SH' AND benchmark_entry.trading_date=e.entry_date
                LEFT JOIN LATERAL (
                  SELECT close FROM quant.canonical_bars_daily bx WHERE bx.symbol='000300.SH' AND bx.trading_date >= e.entry_date
                    AND bx.trading_date <= %s
                  ORDER BY bx.trading_date OFFSET (e.horizon_days - 1) LIMIT 1
                ) benchmark_exit ON true
              ), measured AS (
                SELECT analyst_id,horizon_days,direction,strength,exit_date,
                  ((exit_close / NULLIF(entry_close,0))-1) AS raw_return,
                  ((benchmark_exit_close / NULLIF(benchmark_entry_close,0))-1) AS benchmark_return
                FROM priced WHERE entry_close IS NOT NULL AND exit_close IS NOT NULL
              )
              SELECT analyst_id,horizon_days,count(*)::int observations,
                 avg(CASE WHEN direction*raw_return > 0 THEN 1.0 ELSE 0.0 END) hit_rate,
                 avg(raw_return - coalesce(benchmark_return,0)) mean_excess_return,
                 avg(direction*raw_return) mean_directional_return,
                 avg(1-abs(strength - CASE WHEN direction*raw_return > 0 THEN 1 ELSE 0 END)) calibration_score
              FROM measured WHERE exit_date IS NOT NULL AND exit_date<=%s GROUP BY analyst_id,horizon_days""",
            (as_of_date, as_of_date, as_of_date, as_of_date, as_of_date, as_of_date),
        ).fetchall()
        for row in rows:
            connection.execute(
                """INSERT INTO quant.analyst_scorecards(analyst_id,horizon_days,as_of_date,observations,hit_rate,mean_excess_return,
                   mean_directional_return,calibration_score,methodology_version)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(analyst_id,horizon_days,as_of_date,methodology_version) DO UPDATE SET observations=EXCLUDED.observations,
                   hit_rate=EXCLUDED.hit_rate,mean_excess_return=EXCLUDED.mean_excess_return,
                   mean_directional_return=EXCLUDED.mean_directional_return,calibration_score=EXCLUDED.calibration_score""",
                (row["analyst_id"], row["horizon_days"], as_of_date, row["observations"], row["hit_rate"],
                 row["mean_excess_return"], row["mean_directional_return"], row["calibration_score"], methodology),
            )
        scorecard_readiness = readiness(connection)
    return {"as_of_date": str(as_of_date), "scorecards": len(rows), "methodology_version": methodology,
            "readiness": scorecard_readiness,
            "notice": "仅有方向明确且未来价格路径已结算的股票观点会进入成绩单；主题和中性观点保留为研究上下文。"}


__all__ = ["readiness", "recompute"]
