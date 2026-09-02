"""Local-only analyst scorecard materialization."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Callable

from .market_rules import LIMIT_TOLERANCE
from .outcome_recomputation import resolve_benchmark_close, resolve_exit

#: Bumped from ``excess-return-v1``: entry/exit pricing and the fillability
#: and delisting/suspension handling are now identical to
#: ``outcome_recomputation`` (see the module docstring below) instead of a
#: separate, inconsistent close-to-close computation that also made a 1-day
#: horizon's return identically zero.
METHODOLOGY_VERSION = "excess-return-v2"


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
    """Fold every fillable, settled stock claim into an (analyst, horizon) scorecard row.

    Entry/exit pricing deliberately shares ``outcome_recomputation``'s rules
    instead of a separate close-to-close computation: entry is the next
    session's open (never credited through a locked limit-up/down open or a
    suspended session), and the exit session is resolved from the trade
    calendar and instrument lifecycle rather than the claim's own bar count,
    so a 1-day horizon can no longer exit the same session it entered and a
    suspension gap can no longer silently stretch the holding period.
    """
    as_of_date = as_of_date or cn_today()
    with db.transaction() as connection:
        rows = connection.execute(
            r"""WITH signal_source AS (
                -- Versioned remote archive claims are the sole analyst evidence
                -- input.  The retired analyst_signals table is intentionally
                -- excluded so an old local extraction cannot affect a scorecard.
                SELECT remote_analyst_id AS analyst_id,subject_key AS symbol,direction,strength,horizon_days,available_at
                  FROM quant.analyst_claims
                 WHERE scope='stock' AND subject_key ~ '^\d{6}\.(SH|SZ|BJ)$' AND direction<>0
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
              )
              -- A locked limit-up (long) or limit-down (short-thesis) open, or a
              -- suspended session, is not a fillable entry and is left unsettled
              -- rather than credited/debited as if a real order could have been placed.
              SELECT e.*, entry.open entry_price, benchmark_entry.close AS benchmark_entry_close
                FROM entry_exit e
                JOIN quant.canonical_bars_daily entry ON entry.symbol=e.symbol AND entry.trading_date=e.entry_date
                LEFT JOIN quant.canonical_bars_daily benchmark_entry ON benchmark_entry.symbol='000300.SH' AND benchmark_entry.trading_date=e.entry_date
               WHERE entry.open IS NOT NULL AND NOT entry.is_suspended
                 AND ((e.direction>0 AND (entry.limit_up IS NULL OR entry.open<entry.limit_up-%s))
                   OR (e.direction<0 AND (entry.limit_down IS NULL OR entry.open>entry.limit_down+%s)))""",
            (as_of_date, as_of_date, LIMIT_TOLERANCE, LIMIT_TOLERANCE),
        ).fetchall()
        buckets: dict[tuple[str, int], dict[str, list[float]]] = defaultdict(
            lambda: {"hits": [], "excess": [], "directional": [], "calibration": []}
        )
        for row in rows:
            resolved = resolve_exit(connection, row["symbol"], row["entry_date"], int(row["horizon_days"]), as_of_date)
            if resolved["status"] in ("pending", "suspension_in_window"):
                continue
            entry_price = Decimal(row["entry_price"])
            exit_close = Decimal(resolved["exit_close"])
            raw_return = float(exit_close / entry_price - 1)
            benchmark_exit_close = resolve_benchmark_close(connection, resolved["target_exit_date"])
            benchmark_return = (float(Decimal(benchmark_exit_close) / Decimal(row["benchmark_entry_close"]) - 1)
                                if benchmark_exit_close is not None and row["benchmark_entry_close"] else None)
            direction = int(row["direction"])
            strength = float(row["strength"] or 0.0)
            bucket = buckets[(str(row["analyst_id"]), int(row["horizon_days"]))]
            bucket["hits"].append(1.0 if direction * raw_return > 0 else 0.0)
            bucket["excess"].append(raw_return - (benchmark_return or 0.0))
            bucket["directional"].append(direction * raw_return)
            bucket["calibration"].append(1 - abs(strength - (1.0 if direction * raw_return > 0 else 0.0)))
        scorecards = 0
        for (analyst_id, horizon_days), values in sorted(buckets.items()):
            observations = len(values["hits"])
            if not observations:
                continue
            connection.execute(
                """INSERT INTO quant.analyst_scorecards(analyst_id,horizon_days,as_of_date,observations,hit_rate,mean_excess_return,
                   mean_directional_return,calibration_score,methodology_version)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(analyst_id,horizon_days,as_of_date,methodology_version) DO UPDATE SET observations=EXCLUDED.observations,
                   hit_rate=EXCLUDED.hit_rate,mean_excess_return=EXCLUDED.mean_excess_return,
                   mean_directional_return=EXCLUDED.mean_directional_return,calibration_score=EXCLUDED.calibration_score""",
                (analyst_id, horizon_days, as_of_date, observations,
                 sum(values["hits"]) / observations, sum(values["excess"]) / observations,
                 sum(values["directional"]) / observations, sum(values["calibration"]) / observations, METHODOLOGY_VERSION),
            )
            scorecards += 1
        scorecard_readiness = readiness(connection)
    return {"as_of_date": str(as_of_date), "scorecards": scorecards, "methodology_version": METHODOLOGY_VERSION,
            "readiness": scorecard_readiness,
            "notice": "仅有方向明确且未来价格路径已结算的股票观点会进入成绩单；主题和中性观点保留为研究上下文。"}


__all__ = ["METHODOLOGY_VERSION", "readiness", "recompute"]
