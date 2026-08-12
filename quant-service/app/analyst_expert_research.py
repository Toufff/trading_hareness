"""Point-in-time analyst opinion settlement and Sleeping-Experts research.

This module deliberately implements *research-only* aggregation.  It does not
touch intraday alert thresholds or order logic.  Every opinion starts from the
first time our service received its report, is folded at analyst/day/subject,
and is settled only against data that occurs afterwards.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from statistics import mean, pstdev
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Json


HORIZONS = (1, 2, 3, 5, 10, 20, 40, 60)
OUTCOME_VERSION = "analyst-pit-basket-v1"
EXPERT_VERSION = "sleeping-experts-fixed-share-v1"
EXPERT_DEFAULTS = {"gamma": 0.99, "eta": 0.4, "alpha": 0.01, "kappa": 100}


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _cn_date(value: Any) -> date:
    return value.astimezone(ZoneInfo("Asia/Shanghai")).date() if hasattr(value, "astimezone") else value


def seed_exact_theme_aliases(connection: Any) -> int:
    """Seed only reviewed exact labels; unresolved themes stay unmapped."""
    rows = (
        ("remote:ai应用", "AI应用", "ths_concept_flow", "886108.TI"),
        ("remote:人工智能应用", "人工智能应用", "ths_concept_flow", "886108.TI"),
        ("remote:先进封装", "先进封装", "ths_concept_flow", "886009.TI"),
        ("remote:mlcc", "MLCC", "ths_concept_flow", "886112.TI"),
        ("remote:pcb", "PCB", "ths_concept_flow", "885959.TI"),
        ("remote:黄金", "黄金", "ths_concept_flow", "885530.TI"),
    )
    inserted = 0
    for theme_key, label, taxonomy_key, sector_key in rows:
        exists = connection.execute(
            "SELECT 1 FROM quant.sectors WHERE taxonomy_key=%s AND sector_key=%s", (taxonomy_key, sector_key)
        ).fetchone()
        if not exists:
            continue
        row = connection.execute(
            """INSERT INTO quant.analyst_theme_board_aliases(theme_key,theme_label,taxonomy_key,sector_key,mapping_method,status,metadata)
               VALUES(%s,%s,%s,%s,'reviewed_alias','approved',%s)
               ON CONFLICT(theme_key,taxonomy_key,sector_key) DO UPDATE SET theme_label=EXCLUDED.theme_label,updated_at=now()
               RETURNING theme_key""",
            (theme_key, label, taxonomy_key, sector_key, Json({"reviewed": True})),
        ).fetchone()
        inserted += int(row is not None)
    return inserted


def rebuild_analyst_opinions(connection: Any, as_of_date: date) -> dict[str, Any]:
    """Fold repeated fragments into one analyst × availability-day × subject view."""
    seed_exact_theme_aliases(connection)
    # This is a deterministic materialization, not an append-only signal log.
    # Remove stale folds before rebuilding so a corrected report, mapping or
    # latency classification cannot leave an obsolete eligible opinion behind.
    connection.execute("DELETE FROM quant.analyst_opinions")
    claims = [dict(row) for row in connection.execute(
        """SELECT claim_id,remote_analyst_id,scope,subject_key,subject_label,direction,strength,
                     horizon_days,extraction_confidence,explicitness,published_at,available_at
               FROM quant.analyst_claims
              WHERE available_at::date<=%s
              ORDER BY available_at,claim_id""", (as_of_date,)
    ).fetchall()]
    grouped: dict[tuple[str, date, str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for claim in claims:
        key = (str(claim["remote_analyst_id"]), _cn_date(claim["available_at"]), str(claim["scope"]),
               str(claim["subject_key"]), int(claim["horizon_days"]))
        grouped[key].append(claim)
    statuses: defaultdict[str, int] = defaultdict(int)
    for (analyst, opinion_date, scope, subject_key, horizon), items in grouped.items():
        score = sum(_number(item["direction"]) * _number(item["strength"]) * _number(item["extraction_confidence"]) * _number(item["explicitness"]) for item in items)
        direction = _sign(score)
        weight = sum(max(0.001, _number(item["extraction_confidence"]) * _number(item["explicitness"])) for item in items)
        strength = min(1.0, abs(score) / weight) if weight else 0.0
        explicit = sum(_number(item["explicitness"]) for item in items) / len(items)
        available_at = max(item["available_at"] for item in items)
        published_values = [item["published_at"] for item in items if item.get("published_at")]
        published_at = min(published_values) if published_values else None
        latency = int((available_at - published_at).total_seconds()) if published_at and available_at >= published_at else None
        mapped = scope != "theme" or connection.execute(
            """SELECT 1 FROM quant.analyst_theme_board_aliases
                 WHERE theme_key=%s AND status='approved' LIMIT 1""", (subject_key,)
        ).fetchone() is not None
        # The remote timestamp remains useful for diagnostics, but delayed
        # archival material is never an online training observation.  It can
        # still be viewed/replayed after this explicit downgrade.
        timely = latency is not None and 0 <= latency <= 5 * 60
        factor_status = "neutral" if direction == 0 else ("replay_only" if not timely else ("unmapped" if not mapped else "eligible"))
        label = next((str(item.get("subject_label") or "") for item in items if item.get("subject_label")), subject_key)
        connection.execute(
            """INSERT INTO quant.analyst_opinions(remote_analyst_id,opinion_date,scope,subject_key,subject_label,direction,strength,explicitness,
                    horizon_days,published_at,available_at,latency_seconds,factor_status,source_claim_ids,evidence_count,metadata)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(remote_analyst_id,opinion_date,scope,subject_key,horizon_days) DO UPDATE SET
                 subject_label=EXCLUDED.subject_label,direction=EXCLUDED.direction,strength=EXCLUDED.strength,
                 explicitness=EXCLUDED.explicitness,published_at=EXCLUDED.published_at,available_at=EXCLUDED.available_at,
                 latency_seconds=EXCLUDED.latency_seconds,factor_status=EXCLUDED.factor_status,source_claim_ids=EXCLUDED.source_claim_ids,
                 evidence_count=EXCLUDED.evidence_count,metadata=EXCLUDED.metadata,updated_at=now()""",
            (analyst, opinion_date, scope, subject_key, label, direction, strength, explicit, horizon, published_at, available_at,
             latency, factor_status, Json([str(item["claim_id"]) for item in items]), len(items),
             Json({"fold": "analyst_x_local_availability_day_x_scope_x_subject", "claim_count": len(items)})),
        )
        statuses[factor_status] += 1
    return {"opinions": len(grouped), "factor_status": dict(statuses), "horizons": list(HORIZONS)}


def _next_dates(connection: Any, after_date: date, horizon: int) -> tuple[date | None, date | None]:
    rows = connection.execute(
        """SELECT trading_date FROM quant.canonical_bars_daily WHERE symbol='000001.SH' AND trading_date>%s
             ORDER BY trading_date LIMIT %s""", (after_date, horizon)
    ).fetchall()
    if len(rows) < horizon:
        return (date(rows[0]["trading_date"].year, rows[0]["trading_date"].month, rows[0]["trading_date"].day) if rows else None, None)
    return rows[0]["trading_date"], rows[-1]["trading_date"]


def _basket_symbols(connection: Any, opinion: dict[str, Any]) -> list[str]:
    if opinion["scope"] == "stock":
        return [str(opinion["subject_key"])]
    if opinion["scope"] == "market":
        return ["000001.SH"]
    rows = connection.execute(
        """SELECT DISTINCT m.symbol FROM quant.analyst_theme_board_aliases a
             JOIN quant.sector_membership_history m ON m.taxonomy_key=a.taxonomy_key AND m.sector_key=a.sector_key
            WHERE a.theme_key=%s AND a.status='approved'
              AND m.effective_from<=%s AND (m.effective_to IS NULL OR m.effective_to>=%s)
              AND m.available_at<=%s""",
        (opinion["subject_key"], opinion["opinion_date"], opinion["opinion_date"], opinion["available_at"]),
    ).fetchall()
    return [str(row["symbol"]) for row in rows]


def _basket_return(connection: Any, symbols: list[str], entry_date: date, exit_date: date) -> tuple[float | None, int]:
    if not symbols:
        return None, 0
    rows = connection.execute(
        """SELECT e.symbol,e.close AS entry_close,x.close AS exit_close
             FROM quant.canonical_bars_daily e JOIN quant.canonical_bars_daily x ON x.symbol=e.symbol
            WHERE e.symbol=ANY(%s) AND e.trading_date=%s AND x.trading_date=%s
              AND e.close IS NOT NULL AND x.close IS NOT NULL""", (symbols, entry_date, exit_date)
    ).fetchall()
    returns = [_number(row["exit_close"]) / _number(row["entry_close"]) - 1 for row in rows if _number(row["entry_close"]) > 0]
    return (mean(returns) if returns else None), len(returns)


def recompute_analyst_opinion_outcomes(connection: Any, as_of_date: date) -> dict[str, Any]:
    opinions = [dict(row) for row in connection.execute(
        "SELECT * FROM quant.analyst_opinions WHERE available_at::date<=%s", (as_of_date,)
    ).fetchall()]
    result: defaultdict[str, int] = defaultdict(int)
    for opinion in opinions:
        symbols = _basket_symbols(connection, opinion) if opinion["factor_status"] == "eligible" else []
        for horizon in HORIZONS:
            entry_date, exit_date = _next_dates(connection, opinion["available_at"].date(), horizon)
            status = "pending" if exit_date is None else "matured"
            raw_return = benchmark_return = residual_return = directional_return = None
            basket_size = 0
            if status == "matured":
                raw_return, basket_size = _basket_return(connection, symbols, entry_date, exit_date)
                benchmark_return, _ = _basket_return(connection, ["000001.SH"], entry_date, exit_date)
                if raw_return is None:
                    status = "unavailable"
                else:
                    residual_return = raw_return - (benchmark_return or 0.0)
                    directional_return = _number(opinion["direction"]) * residual_return
            connection.execute(
                """INSERT INTO quant.analyst_opinion_outcomes(opinion_id,horizon_days,entry_date,exit_date,basket_size,raw_return,benchmark_return,
                     residual_return,directional_return,status,methodology_version,metadata,settled_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s='matured' THEN now() ELSE NULL END)
                   ON CONFLICT(opinion_id,horizon_days,methodology_version) DO UPDATE SET entry_date=EXCLUDED.entry_date,exit_date=EXCLUDED.exit_date,
                     basket_size=EXCLUDED.basket_size,raw_return=EXCLUDED.raw_return,benchmark_return=EXCLUDED.benchmark_return,
                     residual_return=EXCLUDED.residual_return,directional_return=EXCLUDED.directional_return,status=EXCLUDED.status,
                     metadata=EXCLUDED.metadata,settled_at=EXCLUDED.settled_at,updated_at=now()""",
                (opinion["opinion_id"], horizon, entry_date, exit_date, basket_size, raw_return, benchmark_return, residual_return,
                 directional_return, status, OUTCOME_VERSION, Json({"basket": opinion["scope"], "point_in_time": True}), status),
            )
            result[status] += 1
    return {"opinions": len(opinions), "outcomes": dict(result), "methodology": OUTCOME_VERSION}


def equal_weight_baseline(connection: Any) -> dict[str, Any]:
    rows = [dict(row) for row in connection.execute(
        """SELECT o.horizon_days,p.opinion_date,p.remote_analyst_id,p.subject_key,p.direction,p.strength,p.explicitness,o.residual_return,o.directional_return
             FROM quant.analyst_opinion_outcomes o JOIN quant.analyst_opinions p ON p.opinion_id=o.opinion_id
            WHERE o.status='matured' AND o.methodology_version=%s AND p.factor_status='eligible'""", (OUTCOME_VERSION,)
    ).fetchall()]
    by_horizon: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_horizon[int(row["horizon_days"])].append(row)
    curves: list[dict[str, Any]] = []
    for horizon in HORIZONS:
        values = by_horizon[horizon]
        daily: dict[date, list[tuple[float, float]]] = defaultdict(list)
        for row in values:
            forecast = _number(row["direction"]) * _number(row["strength"]) * _number(row["explicitness"])
            daily[row["opinion_date"]].append((forecast, _number(row["residual_return"])))
        ics = []
        for pairs in daily.values():
            if len(pairs) < 2:
                continue
            xs, ys = zip(*pairs)
            sx, sy = pstdev(xs), pstdev(ys)
            if sx > 0 and sy > 0:
                ics.append(sum((x - mean(xs)) * (y - mean(ys)) for x, y in pairs) / (len(pairs) * sx * sy))
        ic = mean(ics) if ics else None
        se = pstdev(ics) / math.sqrt(len(ics)) if len(ics) > 1 else None
        curves.append({"horizon_days": horizon, "observations": len(values), "date_clusters": len(ics),
                       "mean_directional_residual": round(mean([_number(v["directional_return"]) for v in values]), 6) if values else None,
                       "equal_weight_hit_rate": round(mean([1.0 if _number(v["directional_return"]) > 0 else 0.0 for v in values]), 5) if values else None,
                       "ic": round(ic, 6) if ic is not None else None, "date_cluster_se": round(se, 6) if se else None,
                       "t_stat": round(ic / se, 4) if ic is not None and se else None})
    prior = None
    reversal = None
    for point in curves:
        value = point["mean_directional_residual"]
        if value is not None and prior is not None and value < prior and reversal is None:
            reversal = point["horizon_days"]
        if value is not None:
            prior = value
    return {"model": "equal_weight_baseline_v1", "status": "research_only", "horizon_curve": curves,
            "car_turning_horizon": reversal, "audience_interaction": {"status": "unavailable", "reason": "no point-in-time audience exposure dataset"}}


def sleeping_experts_fixed_share(connection: Any, as_of_date: date) -> dict[str, Any]:
    rows = [dict(row) for row in connection.execute(
        """SELECT p.remote_analyst_id,p.opinion_date,p.subject_key,p.direction,p.strength,p.explicitness,o.directional_return
             FROM quant.analyst_opinion_outcomes o JOIN quant.analyst_opinions p ON p.opinion_id=o.opinion_id
            WHERE o.status='matured' AND o.horizon_days=5 AND p.factor_status='eligible' AND p.opinion_date<=%s
            ORDER BY p.opinion_date,p.remote_analyst_id""", (as_of_date,)
    ).fetchall()]
    analysts = sorted({str(row["remote_analyst_id"]) for row in rows})
    scores = {analyst: 0.0 for analyst in analysts}
    daily = defaultdict(list)
    for row in rows:
        daily[row["opinion_date"]].append(row)
    equal_daily: list[float] = []
    expert_daily: list[float] = []
    for _, opinions in sorted(daily.items()):
        active = sorted({str(row["remote_analyst_id"]) for row in opinions})
        if not active:
            continue
        raw = [math.exp(EXPERT_DEFAULTS["eta"] * scores[analyst]) for analyst in active]
        denom = sum(raw) or 1.0
        weights = {analyst: ((1 - EXPERT_DEFAULTS["alpha"]) * value / denom + EXPERT_DEFAULTS["alpha"] / len(active)) for analyst, value in zip(active, raw)}
        rewards = defaultdict(list)
        for row in opinions:
            rewards[str(row["remote_analyst_id"])].append(_number(row["directional_return"]))
        expert_daily.append(sum(weights[a] * mean(rewards[a]) for a in active if rewards[a]))
        equal_daily.append(mean([value for group in rewards.values() for value in group]))
        average_reward = mean([mean(values) for values in rewards.values()]) if rewards else 0.0
        for analyst in scores:
            reward = mean(rewards[analyst]) if analyst in rewards else average_reward
            scores[analyst] = EXPERT_DEFAULTS["gamma"] * scores[analyst] + reward
    t_eff = len(expert_daily)
    shrink = t_eff / (t_eff + EXPERT_DEFAULTS["kappa"]) if t_eff else 0.0
    spread = mean(expert_daily) - mean(equal_daily) if expert_daily else None
    status = "eligible_for_review" if t_eff >= 60 and spread is not None and spread > 0 else "research_only"
    result = {"model": EXPERT_VERSION, "defaults": EXPERT_DEFAULTS, "status": status, "settled_date_clusters": t_eff,
              "shrink_to_equal_weight": round(shrink, 6), "walk_forward": {"expert_mean_reward": round(mean(expert_daily), 6) if expert_daily else None,
              "equal_weight_mean_reward": round(mean(equal_daily), 6) if equal_daily else None, "difference": round(spread, 6) if spread is not None else None},
              "scores": {analyst: round(score, 6) for analyst, score in scores.items()},
              "promotion": "disabled until walk-forward beats equal-weight with adequate date clusters"}
    connection.execute(
        """INSERT INTO quant.analyst_expert_runs(as_of_date,model_version,status,result) VALUES(%s,%s,%s,%s)
           ON CONFLICT(as_of_date,model_version) DO UPDATE SET status=EXCLUDED.status,result=EXCLUDED.result""",
        (as_of_date, EXPERT_VERSION, status, Json(result)),
    )
    return result


def rebuild_analyst_research(connection: Any, as_of_date: date) -> dict[str, Any]:
    opinions = rebuild_analyst_opinions(connection, as_of_date)
    outcomes = recompute_analyst_opinion_outcomes(connection, as_of_date)
    baseline = equal_weight_baseline(connection)
    experts = sleeping_experts_fixed_share(connection, as_of_date)
    settled = sum(point["observations"] for point in baseline["horizon_curve"])
    return {"as_of_date": str(as_of_date), "opinions": opinions, "outcomes": outcomes, "equal_weight": baseline,
            "sleeping_experts": experts, "phase_3": {"status": "disabled", "required_settled_outcomes": 5000,
            "current_horizon_observations": settled, "methods": ["conditioned_selection", "pairwise_ranking"],
            "reason": "insufficient settled point-in-time outcomes"}, "live_strategy_effect": "none"}


def analyst_research_status(database: Any, as_of_date: date | None = None) -> dict[str, Any]:
    with database.transaction() as connection:
        latest = connection.execute(
            "SELECT as_of_date,status,result,created_at FROM quant.analyst_expert_runs ORDER BY as_of_date DESC LIMIT 1"
        ).fetchone()
        counts = connection.execute(
            "SELECT factor_status,count(*)::int count FROM quant.analyst_opinions GROUP BY factor_status ORDER BY factor_status"
        ).fetchall()
        mappings = connection.execute("SELECT count(*)::int count FROM quant.analyst_theme_board_aliases WHERE status='approved'").fetchone()
    return {"as_of_date": str(as_of_date) if as_of_date else None, "latest_expert_run": dict(latest) if latest else None,
            "opinion_status_counts": [dict(row) for row in counts], "approved_theme_board_aliases": int(mappings["count"]),
            "boundary": "first local receipt only; research-only; no media fetching; no live strategy weight"}
