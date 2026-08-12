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
from statistics import mean, pstdev, stdev
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Json


HORIZONS = (1, 2, 3, 5, 10, 20, 40, 60)
OUTCOME_VERSION = "analyst-pit-basket-v1"
EXPERT_VERSION = "sleeping-experts-fixed-share-v1"
RESEARCH_VERSION = "analyst-research-evaluation-v2"
EXPERT_DEFAULTS = {"gamma": 0.99, "eta": 0.4, "alpha": 0.01, "kappa": 100}
MIN_BASKET_MEMBERS = 12
MAX_BASKET_MEMBERS = 400
TIMELY_LATENCY_SECONDS = 5 * 60


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
        ("remote:有色金属", "有色金属", "ths_concept_flow", "885552.TI"),
        ("remote:有色铜", "有色铜", "ths_concept_flow", "885973.TI"),
        ("remote:铜加工", "铜加工", "ths_concept_flow", "885973.TI"),
        ("remote:金属钨", "金属钨", "ths_concept_flow", "885552.TI"),
        ("remote:金矿", "金矿", "ths_concept_flow", "885530.TI"),
        # These broad aliases are deliberately reviewed at the related-board
        # level.  Ambiguous terms such as electronic cloth/specialty gas stay
        # unmapped until a human approves a narrower board.
        ("remote:硬件科技", "硬件科技", "ths_index_i", "700338.TI"),
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
        timely = latency is not None and 0 <= latency <= TIMELY_LATENCY_SECONDS
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


def _basket_volatility(connection: Any, symbols: list[str], entry_date: date, exit_date: date) -> float | None:
    """Equal-weight daily return volatility over the realized holding window."""
    if not symbols:
        return None
    rows = connection.execute(
        """WITH prices AS (
                 SELECT symbol,trading_date,close,lag(close) OVER (PARTITION BY symbol ORDER BY trading_date) previous_close
                   FROM quant.canonical_bars_daily
                  WHERE symbol=ANY(%s) AND trading_date BETWEEN %s - interval '7 days' AND %s
             )
             SELECT trading_date,avg(close / NULLIF(previous_close,0) - 1) AS basket_return
               FROM prices WHERE trading_date>=%s AND previous_close IS NOT NULL
              GROUP BY trading_date ORDER BY trading_date""",
        (symbols, entry_date, exit_date, entry_date),
    ).fetchall()
    values = [_number(row["basket_return"]) for row in rows]
    return pstdev(values) if len(values) >= 2 else None


def _industry_size_benchmark_return(connection: Any, opinion: dict[str, Any], entry_date: date,
                                    exit_date: date, fallback_return: float | None) -> tuple[float | None, str]:
    """Construct a PIT peer benchmark where an industry and size control exists.

    Board and market opinions are already baskets, so a broad-market residual
    is the honest fallback.  A stock opinion can use same-industry names in a
    market-cap tercile from the *entry* date.  No inferred classifications are
    used if daily fundamentals are missing.
    """
    if opinion["scope"] != "stock":
        return fallback_return, "broad_market_fallback"
    target = connection.execute(
        """SELECT i.industry,f.circ_mv FROM quant.instruments i
             LEFT JOIN quant.daily_fundamentals f ON f.symbol=i.symbol AND f.trading_date=%s
            WHERE i.symbol=%s""", (entry_date, opinion["subject_key"])
    ).fetchone()
    if not target or not target["industry"] or target["circ_mv"] is None:
        return fallback_return, "broad_market_fallback"
    peers = connection.execute(
        """WITH candidate AS (
                 SELECT i.symbol,f.circ_mv,ntile(3) OVER (ORDER BY f.circ_mv) size_bucket
                   FROM quant.instruments i JOIN quant.daily_fundamentals f ON f.symbol=i.symbol AND f.trading_date=%s
                  WHERE i.industry=%s AND f.circ_mv IS NOT NULL
             ), target_bucket AS (
                 SELECT size_bucket FROM candidate WHERE symbol=%s
             )
             SELECT e.close AS entry_close,x.close AS exit_close
               FROM candidate c JOIN target_bucket t ON t.size_bucket=c.size_bucket
               JOIN quant.canonical_bars_daily e ON e.symbol=c.symbol AND e.trading_date=%s
               JOIN quant.canonical_bars_daily x ON x.symbol=c.symbol AND x.trading_date=%s
              WHERE c.symbol<>%s AND e.close IS NOT NULL AND x.close IS NOT NULL""",
        (entry_date, target["industry"], opinion["subject_key"], entry_date, exit_date, opinion["subject_key"]),
    ).fetchall()
    values = [_number(row["exit_close"]) / _number(row["entry_close"]) - 1 for row in peers if _number(row["entry_close"]) > 0]
    return (mean(values), "industry_size_peer") if len(values) >= 8 else (fallback_return, "broad_market_fallback")


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
            volatility = normalized_reward = None
            if status == "matured":
                raw_return, basket_size = _basket_return(connection, symbols, entry_date, exit_date)
                broad_market_return, _ = _basket_return(connection, ["000001.SH"], entry_date, exit_date)
                benchmark_return, neutralization_method = _industry_size_benchmark_return(
                    connection, opinion, entry_date, exit_date, broad_market_return,
                )
                if raw_return is None or (opinion["scope"] == "theme" and basket_size < MIN_BASKET_MEMBERS):
                    status = "unavailable"
                else:
                    residual_return = raw_return - (benchmark_return or 0.0)
                    directional_return = _number(opinion["direction"]) * residual_return
                    volatility = _basket_volatility(connection, symbols, entry_date, exit_date)
                    # A bounded, risk-adjusted reward.  Cross-sectional
                    # de-consensus is applied in the aggregation layer.
                    normalized_reward = max(-1.0, min(1.0, directional_return / max(volatility or 0.02, 0.005) / 3.0))
            connection.execute(
                """INSERT INTO quant.analyst_opinion_outcomes(opinion_id,horizon_days,entry_date,exit_date,basket_size,raw_return,benchmark_return,
                     residual_return,directional_return,volatility,normalized_reward,status,methodology_version,metadata,settled_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s='matured' THEN now() ELSE NULL END)
                   ON CONFLICT(opinion_id,horizon_days,methodology_version) DO UPDATE SET entry_date=EXCLUDED.entry_date,exit_date=EXCLUDED.exit_date,
                     basket_size=EXCLUDED.basket_size,raw_return=EXCLUDED.raw_return,benchmark_return=EXCLUDED.benchmark_return,
                     residual_return=EXCLUDED.residual_return,directional_return=EXCLUDED.directional_return,volatility=EXCLUDED.volatility,
                     normalized_reward=EXCLUDED.normalized_reward,status=EXCLUDED.status,
                     metadata=EXCLUDED.metadata,settled_at=EXCLUDED.settled_at,updated_at=now()""",
                (opinion["opinion_id"], horizon, entry_date, exit_date, basket_size, raw_return, benchmark_return, residual_return,
                 directional_return, volatility, normalized_reward, status, OUTCOME_VERSION,
                 Json({"basket": opinion["scope"], "point_in_time": True, "min_theme_members": MIN_BASKET_MEMBERS,
                       "neutralization_method": neutralization_method if status == "matured" else "not_settled"}), status),
            )
            result[status] += 1
    return {"opinions": len(opinions), "outcomes": dict(result), "methodology": OUTCOME_VERSION}


def _clustered_mean(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"mean": None, "se": None, "t_stat": None, "clusters": 0}
    average = mean(values)
    se = stdev(values) / math.sqrt(len(values)) if len(values) > 1 else None
    return {"mean": average, "se": se, "t_stat": average / se if se else None, "clusters": len(values)}


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    xs, ys = zip(*pairs)
    sx, sy = pstdev(xs), pstdev(ys)
    return sum((x - mean(xs)) * (y - mean(ys)) for x, y in pairs) / (len(pairs) * sx * sy) if sx and sy else None


def _mature_outcome_rows(connection: Any, as_of_date: date | None = None) -> list[dict[str, Any]]:
    predicate, params = "", [OUTCOME_VERSION]
    if as_of_date is not None:
        predicate, params = "AND p.opinion_date<=%s", [OUTCOME_VERSION, as_of_date]
    return [dict(row) for row in connection.execute(
        f"""SELECT o.horizon_days,o.entry_date,o.exit_date,o.residual_return,o.directional_return,o.normalized_reward,o.volatility,
                    p.opinion_date,p.remote_analyst_id,p.scope,p.subject_key,p.direction,p.strength,p.explicitness
              FROM quant.analyst_opinion_outcomes o JOIN quant.analyst_opinions p ON p.opinion_id=o.opinion_id
             WHERE o.status='matured' AND o.methodology_version=%s AND p.factor_status='eligible' {predicate}""", params,
    ).fetchall()]


def _audience_profile_map(connection: Any) -> dict[str, dict[str, Any]]:
    return {str(row["remote_analyst_id"]): dict(row) for row in connection.execute(
        "SELECT remote_analyst_id,independence_class,audience_size,audience_as_of FROM quant.analyst_research_profiles"
    ).fetchall()}


def equal_weight_baseline(connection: Any) -> dict[str, Any]:
    rows = _mature_outcome_rows(connection)
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
        ics = [value for value in (_pearson(pairs) for pairs in daily.values()) if value is not None]
        ic_stats = _clustered_mean(ics)
        directional = [_number(v["directional_return"]) for v in values]
        curves.append({"horizon_days": horizon, "observations": len(values), "date_clusters": int(ic_stats["clusters"]),
                       "mean_directional_residual": round(mean(directional), 6) if directional else None,
                       "equal_weight_hit_rate": round(mean([1.0 if value > 0 else 0.0 for value in directional]), 5) if directional else None,
                       "ic": round(float(ic_stats["mean"]), 6) if ic_stats["mean"] is not None else None,
                       "date_cluster_se": round(float(ic_stats["se"]), 6) if ic_stats["se"] is not None else None,
                       "t_stat": round(float(ic_stats["t_stat"]), 4) if ic_stats["t_stat"] is not None else None})
    prior, reversal = None, None
    increments: list[dict[str, Any]] = []
    for point in curves:
        current = point["mean_directional_residual"]
        if current is not None and prior is not None:
            increment = current - prior
            increments.append({"horizon_days": point["horizon_days"], "incremental_directional_residual": round(increment, 6)})
            if increment < 0 and reversal is None:
                reversal = point["horizon_days"]
        if current is not None:
            prior = current
    analyst_rows: list[dict[str, Any]] = []
    for analyst in sorted({str(row["remote_analyst_id"]) for row in rows}):
        values = [row for row in rows if str(row["remote_analyst_id"]) == analyst]
        analyst_rows.append({"analyst_id": analyst, "observations": len(values),
                             "mean_directional_residual": round(mean([_number(x["directional_return"]) for x in values]), 6) if values else None,
                             "hit_rate": round(mean([1.0 if _number(x["directional_return"]) > 0 else 0.0 for x in values]), 5) if values else None})
    ranked = sorted((row for row in analyst_rows if row["observations"] >= 10), key=lambda row: row["mean_directional_residual"] or -99, reverse=True)
    top_ids = {row["analyst_id"] for row in ranked[:max(1, len(ranked) // 2)]}
    top_values = [_number(row["directional_return"]) for row in rows if row["remote_analyst_id"] in top_ids]
    rest_values = [_number(row["directional_return"]) for row in rows if row["remote_analyst_id"] not in top_ids]
    profiles = _audience_profile_map(connection)
    audience_values: defaultdict[str, list[float]] = defaultdict(list)
    for row in rows:
        profile = profiles.get(str(row["remote_analyst_id"]), {})
        size = profile.get("audience_size")
        if size is not None:
            bucket = "large" if int(size) >= 100000 else "small"
            audience_values[bucket].append(_number(row["directional_return"]))
    audience_status = "completed" if len(audience_values) >= 2 else "unavailable"
    return {"model": "equal_weight_baseline_v2", "status": "research_only", "horizon_curve": curves,
            "drift_reversal": {"car_turning_horizon": reversal, "increments": increments,
                                 "interpretation": "first horizon where mean directional residual CAR weakens"},
            "analyst_stratification": {"analysts": analyst_rows, "top_vs_rest": {"status": "completed" if ranked else "insufficient_samples",
                "top_mean": round(mean(top_values), 6) if top_values else None, "rest_mean": round(mean(rest_values), 6) if rest_values else None,
                "eligible_analysts": len(ranked)}},
            "audience_interaction": {"status": audience_status,
                "small_mean": round(mean(audience_values["small"]), 6) if audience_values["small"] else None,
                "large_mean": round(mean(audience_values["large"]), 6) if audience_values["large"] else None,
                "reason": None if audience_status == "completed" else "no reviewed point-in-time audience-size profiles"},
            "go_no_go": {"status": "go" if any((point["t_stat"] or 0) >= 1.96 and (point["ic"] or 0) > 0 for point in curves) else "stop_or_collect",
                         "rule": "advance only if positive IC is significant using date-cluster standard errors"}}


def _softmax_weights(scores: dict[str, float], profiles: dict[str, dict[str, Any]]) -> dict[str, float]:
    if not scores:
        return {}
    priors = {"independent": 1.0, "institutional": 0.9, "unknown": 0.75, "promotional": 0.5}
    log_values = {analyst: EXPERT_DEFAULTS["eta"] * score + math.log(priors.get(str(profiles.get(analyst, {}).get("independence_class") or "unknown"), 0.75))
                  for analyst, score in scores.items()}
    maximum = max(log_values.values())
    exps = {analyst: math.exp(value - maximum) for analyst, value in log_values.items()}
    denominator = sum(exps.values()) or 1.0
    count = len(exps)
    return {analyst: (1 - EXPERT_DEFAULTS["alpha"]) * value / denominator + EXPERT_DEFAULTS["alpha"] / count for analyst, value in exps.items()}


def _active_opinion_scores(connection: Any, as_of_date: date, weights: dict[str, float]) -> list[dict[str, Any]]:
    rows = [dict(row) for row in connection.execute(
        """SELECT remote_analyst_id,scope,subject_key,subject_label,direction,explicitness,strength,horizon_days,available_at
             FROM quant.analyst_opinions
            WHERE factor_status='eligible' AND available_at::date<=%s
              AND opinion_date + horizon_days >= %s AND direction<>0
            ORDER BY available_at DESC""", (as_of_date, as_of_date),
    ).fetchall()]
    grouped: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["scope"]), str(row["subject_key"]))].append(row)
    result: list[dict[str, Any]] = []
    for (scope, subject), opinions in grouped.items():
        consensus = mean(_number(row["direction"]) * _number(row["explicitness"]) for row in opinions)
        active_weight = sum(weights.get(str(row["remote_analyst_id"]), 0.0) for row in opinions)
        if not active_weight:
            continue
        score = sum(weights.get(str(row["remote_analyst_id"]), 0.0) *
                    (_number(row["direction"]) * _number(row["explicitness"]) - consensus) for row in opinions) / active_weight
        result.append({"scope": scope, "subject_key": subject, "subject_label": opinions[0]["subject_label"],
                       "analyst_count": len(opinions), "consensus": round(consensus, 6), "deconsensed_score": round(score, 6),
                       "analysts": [str(row["remote_analyst_id"]) for row in opinions]})
    return sorted(result, key=lambda row: abs(float(row["deconsensed_score"])), reverse=True)[:100]


def sleeping_experts_fixed_share(connection: Any, as_of_date: date) -> dict[str, Any]:
    rows = [row for row in _mature_outcome_rows(connection, as_of_date) if int(row["horizon_days"]) == 5]
    analysts = sorted({str(row["remote_analyst_id"]) for row in rows})
    profiles = _audience_profile_map(connection)
    scores = {analyst: 0.0 for analyst in analysts}
    settled_counts = {analyst: 0 for analyst in analysts}
    by_settlement: defaultdict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("exit_date"):
            by_settlement[row["exit_date"]].append(row)
    equal_daily: list[float] = []
    expert_daily: list[float] = []
    for _, opinions in sorted(by_settlement.items()):
        weights = _softmax_weights(scores, profiles)
        raw_rewards = [_number(row["normalized_reward"]) for row in opinions]
        cross_mean = mean(raw_rewards) if raw_rewards else 0.0
        rewards: defaultdict[str, list[float]] = defaultdict(list)
        for row in opinions:
            reward = max(-1.0, min(1.0, _number(row["normalized_reward"]) - cross_mean))
            rewards[str(row["remote_analyst_id"])].append(reward)
        active = sorted(rewards)
        if not active:
            continue
        active_total = sum(weights.get(analyst, 0.0) for analyst in active) or 1.0
        expert_daily.append(sum(weights.get(analyst, 0.0) / active_total * mean(rewards[analyst]) for analyst in active))
        equal_daily.append(mean([mean(values) for values in rewards.values()]))
        for analyst in scores:
            # Sleeping-expert semantics: inactive analysts decay with the
            # non-stationary environment but never receive a fabricated peer
            # reward for an opinion they did not issue.
            scores[analyst] = EXPERT_DEFAULTS["gamma"] * scores[analyst] + (mean(rewards[analyst]) if analyst in rewards else 0.0)
            settled_counts[analyst] += len(rewards.get(analyst, []))
    final_weights = _softmax_weights(scores, profiles)
    t_eff = mean(list(settled_counts.values())) if settled_counts else 0.0
    shrink = t_eff / (t_eff + EXPERT_DEFAULTS["kappa"]) if t_eff else 0.0
    equal_weight = 1.0 / len(final_weights) if final_weights else 0.0
    shrunk_weights = {analyst: (1 - shrink) * equal_weight + shrink * weight for analyst, weight in final_weights.items()}
    spread = mean(expert_daily) - mean(equal_daily) if expert_daily else None
    difference_stats = _clustered_mean([expert - equal for expert, equal in zip(expert_daily, equal_daily)])
    stability = mean([1.0 if expert > equal else 0.0 for expert, equal in zip(expert_daily, equal_daily)]) if expert_daily else 0.0
    eligible = len(expert_daily) >= 60 and spread is not None and spread > 0 and stability >= 0.55 and (difference_stats["t_stat"] or 0) >= 1.96
    result = {"model": EXPERT_VERSION, "defaults": EXPERT_DEFAULTS, "status": "eligible_for_review" if eligible else "research_only",
              "settled_date_clusters": len(expert_daily), "effective_observations_per_analyst": round(t_eff, 4),
              "shrink_to_equal_weight": round(shrink, 6), "walk_forward": {"expert_mean_reward": round(mean(expert_daily), 6) if expert_daily else None,
              "equal_weight_mean_reward": round(mean(equal_daily), 6) if equal_daily else None, "difference": round(spread, 6) if spread is not None else None,
              "date_cluster_t_stat": round(float(difference_stats["t_stat"]), 4) if difference_stats["t_stat"] is not None else None,
              "win_fraction": round(stability, 5)}, "scores": {analyst: round(score, 6) for analyst, score in scores.items()},
              "weights": {analyst: round(weight, 6) for analyst, weight in shrunk_weights.items()},
              "active_opinion_scores": _active_opinion_scores(connection, as_of_date, shrunk_weights),
              "reward": "clip((directional_residual / realized_volatility / 3) - same_settlement_cross_section_mean, -1, 1)",
              "promotion": "disabled until walk-forward beats equal-weight with >=60 date clusters, positive significance and stable wins"}
    connection.execute(
        """INSERT INTO quant.analyst_expert_runs(as_of_date,model_version,status,result) VALUES(%s,%s,%s,%s)
           ON CONFLICT(as_of_date,model_version) DO UPDATE SET status=EXCLUDED.status,result=EXCLUDED.result""",
        (as_of_date, EXPERT_VERSION, result["status"], Json(result)),
    )
    return result


def conditional_selection_and_pairwise_ranking(connection: Any, as_of_date: date) -> dict[str, Any]:
    """Evaluate P3 methods only after the pre-registered 5,000-outcome gate.

    The code path is deliberately present before the data arrive so that the
    method cannot be silently redesigned after seeing results.  It does not
    make a selection or ranking below the gate.
    """
    outcome_count = connection.execute(
        """SELECT count(DISTINCT o.opinion_id)::int count FROM quant.analyst_opinion_outcomes o
              JOIN quant.analyst_opinions p ON p.opinion_id=o.opinion_id
             WHERE o.status='matured' AND p.factor_status='eligible'"""
    ).fetchone()["count"]
    if outcome_count < 5000:
        return {"status": "disabled", "required_settled_outcomes": 5000, "settled_outcomes": int(outcome_count),
                "methods": {"conditioned_selection": "disabled", "pairwise_ranking": "disabled"},
                "reason": "pre-registered data gate not met; no conditional subgroup mining"}
    rows = _mature_outcome_rows(connection, as_of_date)
    regimes = _market_regimes(connection, {row["opinion_date"] for row in rows})
    by_condition: defaultdict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_condition[(str(row["scope"]), int(row["horizon_days"]), regimes.get(row["opinion_date"], "unknown"))].append(row)
    conditional: list[dict[str, Any]] = []
    for (scope, horizon, regime), values in sorted(by_condition.items()):
        by_analyst: defaultdict[str, list[float]] = defaultdict(list)
        for row in values:
            by_analyst[str(row["remote_analyst_id"])].append(_number(row["directional_return"]))
        for analyst, rewards in by_analyst.items():
            if len(rewards) >= 30:
                conditional.append({"analyst_id": analyst, "scope": scope, "horizon_days": horizon, "market_regime": regime,
                                    "observations": len(rewards), "mean_directional_residual": round(mean(rewards), 6)})
    by_analyst: defaultdict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_analyst[str(row["remote_analyst_id"])].append(_number(row["directional_return"]))
    rankings = []
    ids = sorted(by_analyst)
    for index, left in enumerate(ids):
        for right in ids[index + 1:]:
            # Ranking uses matched horizon/date observations only when both
            # exist.  This avoids pretending independent samples are a match.
            left_rows = {(row["opinion_date"], row["horizon_days"]): _number(row["directional_return"])
                         for row in rows if row["remote_analyst_id"] == left}
            right_rows = {(row["opinion_date"], row["horizon_days"]): _number(row["directional_return"])
                          for row in rows if row["remote_analyst_id"] == right}
            shared = sorted(set(left_rows) & set(right_rows))
            if len(shared) < 20:
                continue
            wins = sum(left_rows[key] > right_rows[key] for key in shared)
            rankings.append({"left": left, "right": right, "matched_observations": len(shared),
                             "left_win_rate": round(wins / len(shared), 5)})
    return {"status": "research_only", "required_settled_outcomes": 5000, "settled_outcomes": int(outcome_count),
            "methods": {"conditioned_selection": conditional, "pairwise_ranking": rankings},
            "promotion": "manual review only; never directly alters trading rules"}


def _market_regimes(connection: Any, dates: set[date]) -> dict[date, str]:
    """Classify the broad market using only closes available on each date."""
    if not dates:
        return {}
    rows = connection.execute(
        """SELECT trading_date,close FROM quant.canonical_bars_daily WHERE symbol='000001.SH'
             AND trading_date<=%s ORDER BY trading_date""", (max(dates),)
    ).fetchall()
    closes = [(row["trading_date"], _number(row["close"])) for row in rows if _number(row["close"]) > 0]
    result: dict[date, str] = {}
    for target in dates:
        prior = [(day, close) for day, close in closes if day <= target]
        if len(prior) < 21:
            result[target] = "unknown"
            continue
        returns = [prior[index][1] / prior[index - 1][1] - 1 for index in range(-19, 0)]
        momentum = prior[-1][1] / prior[-21][1] - 1
        volatility = pstdev(returns)
        if volatility >= 0.02:
            result[target] = "high_volatility"
        elif momentum >= 0.03:
            result[target] = "uptrend"
        elif momentum <= -0.03:
            result[target] = "downtrend"
        else:
            result[target] = "range"
    return result


def rebuild_analyst_research(connection: Any, as_of_date: date) -> dict[str, Any]:
    opinions = rebuild_analyst_opinions(connection, as_of_date)
    outcomes = recompute_analyst_opinion_outcomes(connection, as_of_date)
    baseline = equal_weight_baseline(connection)
    experts = sleeping_experts_fixed_share(connection, as_of_date)
    phase_3 = conditional_selection_and_pairwise_ranking(connection, as_of_date)
    result = {"as_of_date": str(as_of_date), "opinions": opinions, "outcomes": outcomes, "equal_weight": baseline,
              "sleeping_experts": experts, "phase_3": phase_3, "live_strategy_effect": "none"}
    status = "eligible_for_review" if experts["status"] == "eligible_for_review" and phase_3["status"] != "disabled" else "research_only"
    connection.execute(
        """INSERT INTO quant.analyst_research_runs(as_of_date,methodology_version,status,result) VALUES(%s,%s,%s,%s)
           ON CONFLICT(as_of_date,methodology_version) DO UPDATE SET status=EXCLUDED.status,result=EXCLUDED.result""",
        (as_of_date, RESEARCH_VERSION, status, Json(result)),
    )
    return result


def analyst_research_status(database: Any, as_of_date: date | None = None) -> dict[str, Any]:
    with database.transaction() as connection:
        latest = connection.execute(
            "SELECT as_of_date,status,result,created_at FROM quant.analyst_expert_runs ORDER BY as_of_date DESC LIMIT 1"
        ).fetchone()
        counts = connection.execute(
            "SELECT factor_status,count(*)::int count FROM quant.analyst_opinions GROUP BY factor_status ORDER BY factor_status"
        ).fetchall()
        mappings = connection.execute("SELECT count(*)::int count FROM quant.analyst_theme_board_aliases WHERE status='approved'").fetchone()
        latest_research = connection.execute(
            "SELECT as_of_date,status,result,created_at FROM quant.analyst_research_runs ORDER BY as_of_date DESC LIMIT 1"
        ).fetchone()
        profiles = connection.execute(
            "SELECT independence_class,count(*)::int count FROM quant.analyst_research_profiles GROUP BY independence_class ORDER BY independence_class"
        ).fetchall()
    return {"as_of_date": str(as_of_date) if as_of_date else None, "latest_expert_run": dict(latest) if latest else None,
            "latest_research_run": dict(latest_research) if latest_research else None,
            "opinion_status_counts": [dict(row) for row in counts], "approved_theme_board_aliases": int(mappings["count"]),
            "analyst_provenance_profiles": [dict(row) for row in profiles],
            "boundary": "first local receipt only; research-only; no media fetching; no live strategy weight"}
