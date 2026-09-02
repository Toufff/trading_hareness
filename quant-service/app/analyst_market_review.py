"""Automated, evidence-only analyst versus market daily/weekly review."""

from __future__ import annotations

import math
import json
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .analyst_market_evaluation import analyst_market_evaluation
from .automation_run_repository import start_run, finish_run, fail_run

CN = ZoneInfo("Asia/Shanghai")
METHODOLOGY_VERSION = "analyst-market-review-v1"
MIN_REGRESSION_DAYS = 8
#: Trading (not calendar) sessions a "weekly" review's displayed period spans.
WEEKLY_TRADING_DAYS = 5
#: Calendar-day lookback for the regression *sample*, independent of the
#: displayed reporting period.  A single natural week (WEEKLY_TRADING_DAYS
#: sessions, one point lost to the forward-return shift below) can never
#: reach MIN_REGRESSION_DAYS=8 pairs; the regression instead draws from a
#: rolling window so the gate is reachable once enough calendar history has
#: accumulated, rather than being architecturally unreachable forever.  Kept
#: under analyst_market_evaluation's 62-day window cap.
REGRESSION_LOOKBACK_DAYS = 60


def ordinary_least_squares(rows: list[dict[str, Any]], x_key: str, y_key: str) -> dict[str, Any]:
    pairs: list[tuple[float, float]] = []
    for row in rows:
        try:
            x, y = float(row[x_key]), float(row[y_key])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(x) and math.isfinite(y):
            pairs.append((x, y))
    if len(pairs) < MIN_REGRESSION_DAYS:
        return {"status": "insufficient_history", "n": len(pairs), "minimum_n": MIN_REGRESSION_DAYS,
                "x": x_key, "y": y_key, "live_effect": "none"}
    n = len(pairs)
    mean_x = sum(x for x, _ in pairs) / n
    mean_y = sum(y for _, y in pairs) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in pairs)
    syy = sum((y - mean_y) ** 2 for _, y in pairs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    slope = sxy / sxx if sxx else 0.0
    intercept = mean_y - slope * mean_x
    r = sxy / math.sqrt(sxx * syy) if sxx and syy else 0.0
    # Standard error and t-stat of the slope, so "descriptive regression" can
    # actually be read as evidence rather than a bare point estimate.
    degrees_of_freedom = n - 2
    residual_sum_squares = max(0.0, syy - slope * sxy)
    residual_variance = residual_sum_squares / degrees_of_freedom if degrees_of_freedom > 0 else None
    se_slope = math.sqrt(residual_variance / sxx) if residual_variance is not None and sxx > 0 else None
    t_stat = slope / se_slope if se_slope else None
    return {"status": "ready", "n": n, "minimum_n": MIN_REGRESSION_DAYS,
            "x": x_key, "y": y_key, "slope": slope, "intercept": intercept,
            "correlation": r, "r_squared": r * r, "se_slope": se_slope, "t_stat": t_stat,
            "degrees_of_freedom": degrees_of_freedom if degrees_of_freedom > 0 else None, "live_effect": "none"}


def _forward_pairs(points: list[dict[str, Any]], x_key: str, y_key: str) -> list[dict[str, Any]]:
    """Pair day N's ``x_key`` with day N+1's ``y_key``.

    A same-day regression of claim direction against the same day's market
    move is contemporaneous/reverse-causal: an analyst reacting to a move
    already in progress looks identical to one predicting it. Shifting
    ``y`` to the next available point in this already trading-day-only
    series is the minimal fix that answers "did today's claims predict
    tomorrow", not "did today's claims describe today".
    """
    ordered = sorted(points, key=lambda row: str(row.get("exchange_date") or ""))
    pairs: list[dict[str, Any]] = []
    for index in range(len(ordered) - 1):
        current, following = ordered[index], ordered[index + 1]
        if current.get(x_key) is not None and following.get(y_key) is not None:
            pairs.append({x_key: current[x_key], y_key: following[y_key]})
    return pairs


def _period(database: Any, cadence: str, as_of: date) -> tuple[date, date]:
    if cadence == "daily":
        return as_of, as_of
    # The report is generated at Friday close.  Resolve the displayed period
    # to the actual last WEEKLY_TRADING_DAYS trading sessions (not a fixed
    # seven-calendar-day span, which can hold as few as one or two sessions
    # across a holiday week) so it reads as what was actually observed.
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT calendar_date FROM quant.market_trade_calendar
                 WHERE exchange='SSE' AND is_open AND calendar_date<=%s
                 ORDER BY calendar_date DESC LIMIT %s""", (as_of, WEEKLY_TRADING_DAYS),
        ).fetchall()
    if rows:
        return min(row["calendar_date"] for row in rows), as_of
    # No calendar coverage this far back (a bootstrap window before the
    # calendar table was backfilled): fall back to the previous heuristic
    # instead of failing the whole review.
    return as_of - timedelta(days=6), as_of


def _market_points(database: Any, start: date, end: date, evaluation: dict[str, Any]) -> list[dict[str, Any]]:
    timeline = {str(row.get("date")): row for row in evaluation.get("timeline", [])}
    with database.transaction() as connection:
        aggregates = [dict(row) for row in connection.execute(
            """SELECT trading_date,stock_count,advancers,decliners,unchanged,
                      median_change_pct,mean_change_pct,total_amount_kcny,source_provider
                 FROM quant.daily_market_aggregates
                WHERE trading_date BETWEEN %s AND %s ORDER BY trading_date""", (start, end)).fetchall()]
        flow_rows = [dict(row) for row in connection.execute(
            """SELECT DISTINCT ON (exchange_date) exchange_date,market_state,
                      concept_positive_ratio,concept_mean_change_pct,amount_change_pct,advancer_ratio
                 FROM quant.market_flow_feature_snapshots
                WHERE exchange_date BETWEEN %s AND %s AND cadence IN ('close','midday')
                ORDER BY exchange_date,CASE cadence WHEN 'close' THEN 0 ELSE 1 END,observed_at DESC""", (start, end)).fetchall()]
    agg = {str(row["trading_date"]): row for row in aggregates}
    flow = {str(row["exchange_date"]): row for row in flow_rows}
    dates = sorted(set(agg) | set(flow) | set(timeline))
    points: list[dict[str, Any]] = []
    for day in dates:
        item = {**(timeline.get(day) or {}), **(flow.get(day) or {}), **(agg.get(day) or {})}
        pos = int((timeline.get(day) or {}).get("positive_claims") or 0)
        neg = int((timeline.get(day) or {}).get("negative_claims") or 0)
        item.update({"exchange_date": day, "positive_claims": pos, "negative_claims": neg,
                     "net_direction_score": pos - neg,
                     "market_mean_change_pct": item.get("mean_change_pct") if item.get("mean_change_pct") is not None else item.get("concept_mean_change_pct")})
        points.append(item)
    return points


def build_analyst_market_review(database: Any, cadence: str, as_of_date: date | None = None) -> dict[str, Any]:
    as_of = as_of_date or datetime.now(CN).date()
    if cadence not in {"daily", "weekly"}:
        raise ValueError("cadence must be daily or weekly")
    start, end = _period(database, cadence, as_of)
    evaluation = analyst_market_evaluation(database, start, end)
    points = _market_points(database, start, end, evaluation)
    regressions = []
    regression_window = None
    if cadence == "weekly":
        # The regression draws from a rolling lookback independent of the
        # displayed period (see REGRESSION_LOOKBACK_DAYS), and each pair
        # shifts y to the next trading day so the fit answers "did this
        # day's claims predict the next day's move", not "describe the same
        # day's move" (see _forward_pairs).
        regression_start = max(end - timedelta(days=REGRESSION_LOOKBACK_DAYS), date(1990, 1, 1))
        regression_evaluation = evaluation if regression_start == start else analyst_market_evaluation(database, regression_start, end)
        regression_points = points if regression_start == start else _market_points(database, regression_start, end, regression_evaluation)
        regression_window = {"start_date": str(regression_start), "end_date": str(end), "points": len(regression_points)}
        for key in ("net_direction_score", "positive_claims", "negative_claims", "concept_positive_ratio"):
            regressions.append(ordinary_least_squares(
                _forward_pairs(regression_points, key, "market_mean_change_pct"), key, "market_mean_change_pct",
            ))
    status = "ready" if points and (cadence == "daily" or any(r.get("status") == "ready" for r in regressions)) else "insufficient_history"
    summary = {"cadence": cadence, "period": {"start_date": str(start), "end_date": str(end)},
               "daily_points": points, "evaluation": evaluation, "regressions": regressions,
               "regression_window": regression_window,
               "governance": {"live_effect": "none", "replay_only_author_actions": True,
                              "notice": "描述性研究；回归样本不足时不拟合、不调实时阈值。y 为次日市场收益，避免同期反向因果。"},
               "generated_at": datetime.now(CN).isoformat()}
    with database.transaction() as connection:
        row = connection.execute(
            """INSERT INTO quant.analyst_market_reviews(cadence,period_start,period_end,status,methodology_version,summary)
                 VALUES(%s,%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT(cadence,period_start,period_end) DO UPDATE SET status=EXCLUDED.status,
                methodology_version=EXCLUDED.methodology_version,summary=EXCLUDED.summary,
                generated_at=now(),updated_at=now() RETURNING review_id,generated_at""",
                # psycopg returns numeric columns as Decimal; stringify only at
                # the JSONB boundary so a provider's numeric precision never
                # makes the automated review fail at close.
                (cadence, start, end, status, METHODOLOGY_VERSION, json.dumps(summary, ensure_ascii=False, default=str)),
        ).fetchone()
    return {
        "review_id": str(row["review_id"]), "cadence": cadence,
        "period_start": str(start), "period_end": str(end),
        "status": status, "methodology_version": METHODOLOGY_VERSION,
        "summary": summary, "generated_at": summary["generated_at"],
    }


def list_analyst_market_reviews(database: Any, cadence: str | None = None, limit: int = 20) -> dict[str, Any]:
    bounded = max(1, min(int(limit), 100))
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT review_id,cadence,period_start,period_end,status,methodology_version,summary,generated_at,updated_at
                 FROM quant.analyst_market_reviews
                WHERE (%s::text IS NULL OR cadence=%s)
                ORDER BY period_end DESC,generated_at DESC LIMIT %s""", (cadence, cadence, bounded)).fetchall()
    return {"items": [dict(row) for row in rows], "live_effect": "none", "methodology_version": METHODOLOGY_VERSION}


def latest_analyst_market_review(database: Any, cadence: str) -> dict[str, Any]:
    result = list_analyst_market_reviews(database, cadence, 1)
    item = result["items"][0] if result["items"] else None
    return {"review": item, "live_effect": "none", "methodology_version": METHODOLOGY_VERSION}


def build_recorded_analyst_market_review(database: Any, cadence: str, as_of_date: date | None = None) -> dict[str, Any]:
    """Run the review with a durable success/failure receipt for automation."""
    as_of = as_of_date or datetime.now(CN).date()
    start, end = _period(database, cadence, as_of)
    run_key = f"analyst-market-review:{cadence}:{start}:{end}"
    with database.transaction() as connection:
        run_id = start_run(
            connection, task_key="analyst_market_review", run_key=run_key,
            cadence=cadence, as_of_date=end, methodology_version=METHODOLOGY_VERSION,
            input_summary={"period_start": str(start), "period_end": str(end)},
        )
    try:
        result = build_analyst_market_review(database, cadence, as_of)
    except Exception as error:
        with database.transaction() as connection:
            fail_run(connection, run_id, error)
        raise
    with database.transaction() as connection:
        finish_run(connection, run_id, output_summary={
            "status": result.get("status"), "review_id": result.get("review_id"),
            "point_count": len((result.get("summary") or {}).get("daily_points") or []),
        })
    return result


__all__ = ["ordinary_least_squares", "build_analyst_market_review", "build_recorded_analyst_market_review", "list_analyst_market_reviews", "latest_analyst_market_review"]
