"""Read-only analyst versus market evaluation.

The append-only ``analyst_observations`` table is the prediction-event ledger.
This module deliberately does not create a second mutable prediction table: it
projects those immutable events together with the point-in-time opinion and
market-flow outcome ledgers.  Results are descriptive until the sample gates
are met and therefore cannot change live strategy weights.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .analyst_calibration import chronological_calibration


CN = ZoneInfo("Asia/Shanghai")
REQUIRED_DAYS = 60
REQUIRED_MATURED_EVENTS = 200


def _as_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.astimezone(CN).date() if value.tzinfo else value.date()
    return date.fromisoformat(str(value))


def _market_alignment(direction: int, state: str | None) -> str:
    """A transparent, coarse label; no numeric score is applied to live rules."""
    if not direction or not state:
        return "neutral_or_missing"
    state = str(state)
    positive = state in {"flow_expansion", "broad_risk_on", "recovery", "mixed_rotation"}
    negative = state in {"flow_risk_off", "broad_risk_off", "contraction"}
    if positive and direction > 0 or negative and direction < 0:
        return "aligned"
    if positive and direction < 0 or negative and direction > 0:
        return "contrarian"
    return "unclassified"


def _return_metrics(values: list[float]) -> dict[str, Any]:
    return {
        "observations": len(values),
        "hit_rate": sum(value > 0 for value in values) / len(values) if values else None,
        "mean_directional_return": sum(values) / len(values) if values else None,
    }


def _coverage_matrix(
    observations: list[dict[str, Any]],
    opinions: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build auditable analyst/scope/horizon coverage cohorts.

    Counts are deliberately kept separate from returns: an unavailable or
    replay-only event is evidence about data coverage, not a negative trade.
    The returned rows are descriptive and never feed a live weight.
    """
    by_scope: dict[tuple[str, str], dict[str, Any]] = {}
    by_horizon: dict[tuple[str, str, int], dict[str, Any]] = {}

    def scope_row(analyst: str, scope: str) -> dict[str, Any]:
        return by_scope.setdefault((analyst, scope), {
            "analyst_id": analyst, "scope": scope, "observations": 0,
            "eligible_observations": 0, "replay_only_observations": 0,
            "unmapped_observations": 0, "neutral_observations": 0,
            "opinions": 0, "directional_opinions": 0,
            "matured_outcomes": 0, "pending_outcomes": 0,
            "unavailable_outcomes": 0, "returns": [],
        })

    for row in observations:
        item = scope_row(str(row.get("analyst_id") or ""), str(row.get("scope") or "unknown"))
        item["observations"] += 1
        status = str(row.get("status") or "")
        field = {
            "eligible": "eligible_observations", "replay_only": "replay_only_observations",
            "unmapped": "unmapped_observations", "neutral": "neutral_observations",
        }.get(status)
        if field:
            item[field] += 1

    opinion_keys: dict[str, tuple[str, str]] = {}
    for row in opinions:
        analyst = str(row.get("remote_analyst_id") or "")
        scope = str(row.get("scope") or "unknown")
        item = scope_row(analyst, scope)
        item["opinions"] += 1
        if int(row.get("direction") or 0):
            item["directional_opinions"] += 1
        opinion_id = row.get("opinion_id")
        if opinion_id is not None:
            opinion_keys[str(opinion_id)] = (analyst, scope)

    for row in outcomes:
        analyst = str(row.get("remote_analyst_id") or "")
        scope = str(row.get("scope") or opinion_keys.get(str(row.get("opinion_id") or ""), ("", "unknown"))[1])
        item = scope_row(analyst, scope)
        status = str(row.get("status") or "unavailable")
        status_field = {"matured": "matured_outcomes", "pending": "pending_outcomes", "unavailable": "unavailable_outcomes"}.get(status)
        if status_field:
            item[status_field] += 1
        if status == "matured" and row.get("directional_return") is not None:
            item["returns"].append(float(row["directional_return"]))
        try:
            horizon = int(row.get("horizon_days") or 0)
        except (TypeError, ValueError):
            horizon = 0
        if horizon:
            cohort = by_horizon.setdefault((analyst, scope, horizon), {
                "analyst_id": analyst, "scope": scope, "horizon_days": horizon,
                "outcomes": 0, "matured": 0, "pending": 0, "unavailable": 0, "returns": [],
            })
            cohort["outcomes"] += 1
            if status in {"matured", "pending", "unavailable"}:
                cohort[status] += 1
            if status == "matured" and row.get("directional_return") is not None:
                cohort["returns"].append(float(row["directional_return"]))

    def finalize(row: dict[str, Any]) -> dict[str, Any]:
        values = row.pop("returns", [])
        row["matured_return_observations"] = len(values)
        row["hit_rate"] = sum(value > 0 for value in values) / len(values) if values else None
        row["mean_directional_return"] = sum(values) / len(values) if values else None
        return row

    return (
        [finalize(row) for row in sorted(by_scope.values(), key=lambda value: (value["analyst_id"], value["scope"]))],
        [finalize(row) for row in sorted(by_horizon.values(), key=lambda value: (value["analyst_id"], value["scope"], value["horizon_days"]))],
    )


def _baseline_comparison(
    outcomes: list[dict[str, Any]], market_by_day: dict[date, dict[str, Any]],
    sector_flow_by_day: dict[tuple[date, str], float],
    theme_board_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compare mature five-day outcomes with pre-specified non-analyst signs."""
    rows = [row for row in outcomes if row.get("status") == "matured" and row.get("horizon_days") == 5
            and row.get("residual_return") is not None]
    buckets: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        day = _as_date(row.get("opinion_date"))
        residual = float(row["residual_return"])
        # Existing analyst directional return is the equal-weight expert
        # baseline for this event; it is not used to train the other signs.
        if row.get("directional_return") is not None:
            buckets["equal_analyst"].append(float(row["directional_return"]))
        market = market_by_day.get(day) or {}
        state = str(market.get("market_state") or "")
        market_sign = 1 if state in {"flow_expansion", "broad_risk_on", "recovery"} else -1 if state in {"flow_risk_off", "broad_risk_off", "contraction"} else 0
        if market_sign:
            buckets["market_state"].append(market_sign * residual)
        subject_key = str(row.get("subject_key") or "")
        mapped_sector_key = (theme_board_map or {}).get(subject_key, subject_key)
        flow = sector_flow_by_day.get((day, mapped_sector_key))
        flow_sign = 1 if flow and flow > 0 else -1 if flow and flow < 0 else 0
        if flow_sign:
            buckets["sector_flow"].append(flow_sign * residual)
        amount_change = market.get("amount_change_pct")
        breadth = market.get("advancer_ratio")
        try:
            price_volume_sign = 1 if float(amount_change) > 0 and float(breadth) >= 0.5 else -1 if float(amount_change) < 0 and float(breadth) < 0.5 else 0
        except (TypeError, ValueError):
            price_volume_sign = 0
        if price_volume_sign:
            buckets["price_volume"].append(price_volume_sign * residual)
    return {key: _return_metrics(values) for key, values in sorted(buckets.items())}


def summarize_evaluation(
    *,
    observations: list[dict[str, Any]],
    opinions: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    intraday_outcomes: list[dict[str, Any]],
    author_action_outcomes: list[dict[str, Any]] | None = None,
    market_days: list[dict[str, Any]],
    sector_days: list[dict[str, Any]],
    market_days_for_baseline: list[dict[str, Any]] | None = None,
    theme_board_map: dict[str, str] | None = None,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """Pure aggregation used by the HTTP read model and unit tests."""
    market_by_day = {_as_date(row["exchange_date"]): row for row in market_days}
    baseline_market_by_day = {_as_date(row["exchange_date"]): row for row in (market_days_for_baseline or market_days)}
    sector_flow_by_day: dict[tuple[date, str], float] = {}
    for row in sector_days:
        if row.get("trading_date") is not None and row.get("sector_key"):
            try:
                sector_flow_by_day[(_as_date(row["trading_date"]), str(row["sector_key"]))] = float(row.get("net_amount") or 0)
            except (TypeError, ValueError):
                continue
    outcome_by_analyst: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"matured": 0, "pending": 0, "unavailable": 0, "returns": [], "event_keys": set()}
    )
    intraday_by_analyst: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "matured": 0, "pending": 0, "unavailable": 0, "returns": [], "event_keys": set(),
    })
    matured_daily_event_keys: set[Any] = set()
    matured_intraday_event_keys: set[Any] = set()
    for row in outcomes:
        analyst = str(row.get("remote_analyst_id") or "")
        status = str(row.get("status") or "unavailable")
        bucket = outcome_by_analyst[analyst]
        bucket[status] = int(bucket.get(status, 0)) + 1
        if status == "matured" and row.get("directional_return") is not None:
            bucket["returns"].append(float(row["directional_return"]))
        if status == "matured":
            # One independent event per opinion_id, not once per horizon_days
            # row: analyst_opinion_outcomes carries one row per (opinion,
            # horizon) pair, so an opinion evaluated at 8 horizons must not
            # count as 8 samples toward the per-analyst maturity gate below.
            event_key = row.get("opinion_id") or (
                analyst, row.get("opinion_date"), row.get("scope"), row.get("subject_key"),
            )
            bucket["event_keys"].add(event_key)
            matured_daily_event_keys.add(event_key)

    # Intraday settlement is a real, session-bounded outcome even when the
    # longer daily horizon is still pending.  Count one independent event per
    # observation_id, not once per 5/15/30/60 minute horizon.  Older fixtures
    # without an analyst_id/observation_id remain visible in the intraday
    # summary but cannot inflate the independent-event gate.
    for row in intraday_outcomes:
        analyst = str(row.get("analyst_id") or "")
        status = str(row.get("status") or "unavailable")
        bucket = intraday_by_analyst[analyst]
        bucket[status] = int(bucket.get(status, 0)) + 1
        if status == "matured" and row.get("directional_return") is not None:
            bucket["returns"].append(float(row["directional_return"]))
        event_key = row.get("observation_id")
        if status == "matured" and event_key is not None:
            key = str(event_key)
            bucket["event_keys"].add(key)
            matured_intraday_event_keys.add(key)

    matured_event_keys = matured_daily_event_keys | matured_intraday_event_keys

    analyst_rows: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "analyst_id": "", "observations": 0, "eligible_observations": 0,
        "replay_only_observations": 0, "neutral_observations": 0,
        "market_claims": 0, "theme_claims": 0, "stock_claims": 0,
        "directional_claims": 0, "first_available_at": None, "latest_available_at": None,
    })
    for row in observations:
        analyst = str(row.get("analyst_id") or "")
        item = analyst_rows[analyst]
        item["analyst_id"] = analyst
        item["observations"] += 1
        status = str(row.get("status") or "")
        key = {"eligible": "eligible_observations", "replay_only": "replay_only_observations", "neutral": "neutral_observations"}.get(status)
        if key:
            item[key] += 1
        scope = str(row.get("scope") or "")
        if scope in {"market", "theme", "stock"}:
            item[f"{scope}_claims"] += 1
        if int(row.get("direction") or 0):
            item["directional_claims"] += 1
        available = row.get("strategy_available_at")
        if available:
            value = available.isoformat() if hasattr(available, "isoformat") else str(available)
            if item["first_available_at"] is None or value < item["first_available_at"]:
                item["first_available_at"] = value
            if item["latest_available_at"] is None or value > item["latest_available_at"]:
                item["latest_available_at"] = value

    for analyst, item in analyst_rows.items():
        result = outcome_by_analyst.get(analyst, {})
        matured = int(result.get("matured", 0))
        matured_independent_events = len(result.get("event_keys", set()))
        item["matured_outcomes"] = matured
        item["matured_independent_events"] = matured_independent_events
        item["pending_outcomes"] = int(result.get("pending", 0))
        item["unavailable_outcomes"] = int(result.get("unavailable", 0))
        values = result.get("returns", [])
        item["mean_directional_return"] = sum(values) / len(values) if values else None
        item["directional_hit_rate"] = sum(1 for value in values if value > 0) / len(values) if values else None
        intraday = intraday_by_analyst.get(analyst, {})
        intraday_values = intraday.get("returns", [])
        item["intraday_matured_outcomes"] = int(intraday.get("matured", 0))
        item["intraday_matured_events"] = len(intraday.get("event_keys", set()))
        item["intraday_pending_outcomes"] = int(intraday.get("pending", 0))
        item["intraday_unavailable_outcomes"] = int(intraday.get("unavailable", 0))
        item["intraday_mean_directional_return"] = sum(intraday_values) / len(intraday_values) if intraday_values else None
        item["intraday_directional_hit_rate"] = sum(1 for value in intraday_values if value > 0) / len(intraday_values) if intraday_values else None
        item["manual_review_status"] = (
            "matured_intraday_available" if item["intraday_matured_events"] else
            "matured_daily_available" if matured else "no_matured_outcome"
        )
        # Gated on independent opinion_id-deduplicated events, not raw outcome
        # rows: analyst_opinion_outcomes carries one row per (opinion,
        # horizon), so 4 opinions evaluated across the 8 registered horizons
        # is 4 samples, not 32.
        item["mature"] = matured_independent_events >= 30 and item["directional_claims"] >= 30
        item["status"] = "eligible_for_review" if item["mature"] else "research_only"
        item["gate_reason"] = None if item["mature"] else "matured independent daily outcome events < 30 or directional claims < 30"

    timeline_map: dict[date, dict[str, Any]] = {}
    for day, market in market_by_day.items():
        if start_date <= day <= end_date:
            timeline_map[day] = {
                "date": str(day), "market_state": market.get("market_state"),
                "market_status": market.get("status"), "concept_positive_ratio": market.get("concept_positive_ratio"),
                "market_amount": market.get("market_amount"), "analyst_claims": 0,
                "positive_claims": 0, "negative_claims": 0, "aligned_claims": 0,
                "contrarian_claims": 0,
            }
    for row in opinions:
        day = _as_date(row.get("opinion_date"))
        if not (start_date <= day <= end_date):
            continue
        timeline_map.setdefault(day, {
            "date": str(day), "market_state": None, "market_status": "missing",
            "concept_positive_ratio": None, "market_amount": None,
            "analyst_claims": 0, "positive_claims": 0, "negative_claims": 0,
            "aligned_claims": 0, "contrarian_claims": 0,
        })
        item = timeline_map[day]
        direction = int(row.get("direction") or 0)
        item["analyst_claims"] += 1
        item["positive_claims"] += direction > 0
        item["negative_claims"] += direction < 0
        alignment = _market_alignment(direction, item.get("market_state"))
        if alignment in {"aligned", "contrarian"}:
            item[f"{alignment}_claims"] += 1
    timeline = [timeline_map[key] for key in sorted(timeline_map)]
    sector_summary: dict[str, dict[str, Any]] = {}
    for row in sector_days:
        key = str(row.get("sector_key") or "")
        if not key:
            continue
        item = sector_summary.setdefault(key, {"sector_key": key, "label": row.get("label"), "days": 0, "positive_days": 0, "negative_days": 0, "net_amount_sum": 0.0, "lhb_negative_sum": 0})
        item["days"] += 1
        amount = row.get("net_amount")
        if amount is not None:
            item["net_amount_sum"] += float(amount)
            item["positive_days"] += float(amount) > 0
            item["negative_days"] += float(amount) < 0
        item["lhb_negative_sum"] += int(row.get("lhb_negative_count") or 0)
    sectors = sorted(sector_summary.values(), key=lambda row: abs(row["net_amount_sum"]), reverse=True)[:20]
    intraday_summary: dict[int, dict[str, Any]] = {}
    intraday_action_summary: dict[tuple[str, int], dict[str, Any]] = {}
    for row in intraday_outcomes:
        horizon = int(row.get("horizon_minutes") or 0)
        item = intraday_summary.setdefault(horizon, {"horizon_minutes": horizon, "matured": 0, "pending": 0, "unavailable": 0, "returns": []})
        status = str(row.get("status") or "unavailable")
        item[status] = int(item.get(status, 0)) + 1
        if status == "matured" and row.get("directional_return") is not None:
            item["returns"].append(float(row["directional_return"]))
        path = row.get("settlement") or {}
        if isinstance(path, dict):
            path = path.get("path") or {}
            if path.get("mfe") is not None:
                item.setdefault("mfe_values", []).append(float(path["mfe"]))
            if path.get("mae") is not None:
                item.setdefault("mae_values", []).append(float(path["mae"]))
        action = str(row.get("action") or "unknown")
        action_item = intraday_action_summary.setdefault((action, horizon), {
            "action": action, "horizon_minutes": horizon, "matured": 0,
            "pending": 0, "unavailable": 0, "returns": [], "mfe_values": [], "mae_values": [],
        })
        action_item[status] = int(action_item.get(status, 0)) + 1
        if status == "matured" and row.get("directional_return") is not None:
            action_item["returns"].append(float(row["directional_return"]))
        if isinstance(path, dict):
            if path.get("mfe") is not None:
                action_item["mfe_values"].append(float(path["mfe"]))
            if path.get("mae") is not None:
                action_item["mae_values"].append(float(path["mae"]))
    for item in intraday_summary.values():
        values = item.pop("returns")
        item["mean_directional_return"] = sum(values) / len(values) if values else None
        item["hit_rate"] = sum(1 for value in values if value > 0) / len(values) if values else None
        mfe_values = item.pop("mfe_values", [])
        mae_values = item.pop("mae_values", [])
        item["mean_mfe"] = sum(mfe_values) / len(mfe_values) if mfe_values else None
        item["mean_mae"] = sum(mae_values) / len(mae_values) if mae_values else None
    for item in intraday_action_summary.values():
        values = item.pop("returns")
        item["mean_directional_return"] = sum(values) / len(values) if values else None
        item["hit_rate"] = sum(1 for value in values if value > 0) / len(values) if values else None
        mfe_values = item.pop("mfe_values")
        mae_values = item.pop("mae_values")
        item["mean_mfe"] = sum(mfe_values) / len(mfe_values) if mfe_values else None
        item["mean_mae"] = sum(mae_values) / len(mae_values) if mae_values else None
    author_action_summary: dict[tuple[str, int], dict[str, Any]] = {}
    for row in author_action_outcomes or []:
        action = str(row.get("action_type") or "unknown")
        horizon = int(row.get("horizon_minutes") or 0)
        item = author_action_summary.setdefault((action, horizon), {
            "analyst_id": str(row.get("analyst_id") or "anqiang-touzi-riji"),
            "action": action, "horizon_minutes": horizon, "matured": 0,
            "pending": 0, "unavailable": 0, "returns": [], "replay_only": True,
        })
        status = str(row.get("status") or "unavailable")
        item[status] = int(item.get(status, 0)) + 1
        if status == "matured" and row.get("directional_return") is not None:
            item["returns"].append(float(row["directional_return"]))
    for item in author_action_summary.values():
        values = item.pop("returns")
        item["mean_directional_return"] = sum(values) / len(values) if values else None
        item["hit_rate"] = sum(1 for value in values if value > 0) / len(values) if values else None
    coverage_matrix, horizon_matrix = _coverage_matrix(observations, opinions, outcomes)
    matured = len(matured_event_keys)
    observed_days = len({str(_as_date(row["exchange_date"])) for row in market_days})
    gate_status = "eligible_for_review" if observed_days >= REQUIRED_DAYS and matured >= REQUIRED_MATURED_EVENTS else "accumulating"

    # Calibrated per horizon, never pooled: analyst_opinion_outcomes carries
    # one row per (opinion, horizon), so pooling every registered horizon
    # into one Platt fit let the same opinion enter the training/OOF sample
    # up to 8 times.  ``exit_date`` (when the horizon's label was actually
    # observable) drives the embargo inside chronological_calibration; it is
    # threaded through here rather than only ``opinion_date``.
    calibration_events_by_horizon: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in outcomes:
        if (row.get("status") != "matured" or row.get("directional_return") is None
                or row.get("opinion_date") is None or row.get("score") is None):
            continue
        try:
            horizon = int(row.get("horizon_days") or 0)
        except (TypeError, ValueError):
            continue
        calibration_events_by_horizon[horizon].append({
            "event_date": row.get("opinion_date"), "exit_date": row.get("exit_date"),
            "score": row.get("score"), "label": 1 if float(row.get("directional_return") or 0) > 0 else 0,
        })
    calibration_by_horizon = {
        str(horizon): chronological_calibration(events)
        for horizon, events in sorted(calibration_events_by_horizon.items())
    }
    _primary_horizon = 5 if 5 in calibration_events_by_horizon else (
        min(calibration_events_by_horizon) if calibration_events_by_horizon else None
    )
    _calibration_by_horizon = calibration_by_horizon.get(str(_primary_horizon), chronological_calibration([]))

    return {
        "window": {"start_date": str(start_date), "end_date": str(end_date), "timezone": "Asia/Shanghai"},
        "analysts": sorted(analyst_rows.values(), key=lambda row: row["analyst_id"]),
        "timeline": timeline,
        "sector_context": sectors,
        "coverage_matrix": coverage_matrix,
        "horizon_matrix": horizon_matrix,
        "intraday_outcomes": sorted(intraday_summary.values(), key=lambda row: row["horizon_minutes"]),
        "intraday_action_outcomes": sorted(intraday_action_summary.values(), key=lambda row: (row["action"], row["horizon_minutes"])),
        "author_action_outcomes": sorted(author_action_summary.values(), key=lambda row: (row["action"], row["horizon_minutes"])),
        "quality_gate": {
            "status": gate_status, "observed_trading_days": observed_days,
            "matured_independent_events": matured, "minimum_trading_days": REQUIRED_DAYS,
            "minimum_independent_events": REQUIRED_MATURED_EVENTS, "live_strategy_effect": "none",
            "notice": "研究样本未达到门禁时只展示，不改变实时策略权重。",
            "matured_daily_independent_events": len(matured_daily_event_keys),
            "matured_intraday_independent_events": len(matured_intraday_event_keys),
        },
        "event_ledger": {"observations": len(observations), "opinions": len(opinions), "outcomes": len(outcomes), "intraday_outcomes": len(intraday_outcomes), "matured_independent_events": matured, "matured_daily_independent_events": len(matured_daily_event_keys), "matured_intraday_independent_events": len(matured_intraday_event_keys), "append_only_source": "quant.analyst_observations"},
        "calibration": _calibration_by_horizon,
        "calibration_by_horizon": calibration_by_horizon,
        "baselines": _baseline_comparison(outcomes, baseline_market_by_day, sector_flow_by_day, theme_board_map),
    }


def analyst_market_evaluation(database: Any, start_date: date | None = None, end_date: date | None = None, analyst_id: str | None = None) -> dict[str, Any]:
    end = end_date or datetime.now(CN).date()
    start = start_date or (end - timedelta(days=14))
    if end < start or (end - start).days > 62:
        raise ValueError("evaluation window must be ordered and no longer than 62 days")
    with database.transaction() as connection:
        observations = [dict(row) for row in connection.execute(
            """SELECT analyst_id,source_kind,strategy_available_at,scope,action,direction,status,subject_key,subject_label
                 FROM quant.analyst_observations
                WHERE strategy_available_at >= %s::date AND strategy_available_at < (%s::date + interval '1 day')
                  AND (%s::text IS NULL OR analyst_id=%s)""", (start, end, analyst_id, analyst_id)).fetchall()]
        opinions = [dict(row) for row in connection.execute(
            """SELECT remote_analyst_id,opinion_date,scope,subject_key,direction,strength,factor_status
                 FROM quant.analyst_opinions
                WHERE opinion_date BETWEEN %s AND %s AND (%s::text IS NULL OR remote_analyst_id=%s)""", (start, end, analyst_id, analyst_id)).fetchall()]
        outcomes = [dict(row) for row in connection.execute(
            """SELECT o.opinion_id,p.remote_analyst_id,p.opinion_date,p.scope,p.subject_key,
                            p.direction * p.strength * p.explicitness score,
                            o.status,o.directional_return,o.residual_return,o.horizon_days,o.exit_date
                 FROM quant.analyst_opinion_outcomes o JOIN quant.analyst_opinions p ON p.opinion_id=o.opinion_id
                WHERE p.opinion_date BETWEEN %s AND %s AND (%s::text IS NULL OR p.remote_analyst_id=%s)""", (start, end, analyst_id, analyst_id)).fetchall()]
        intraday_outcomes = [dict(row) for row in connection.execute(
            """SELECT io.observation_id,ob.analyst_id,ob.scope,ob.subject_key,ob.action,ob.direction,
                          io.horizon_minutes,io.status,io.directional_return,io.settlement
                 FROM quant.analyst_intraday_outcomes io JOIN quant.analyst_observations ob ON ob.observation_id=io.observation_id
                WHERE (ob.strategy_available_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                  AND (%s::text IS NULL OR ob.analyst_id=%s)""", (start, end, analyst_id, analyst_id)).fetchall()]
        author_action_outcomes = [dict(row) for row in connection.execute(
            """SELECT a.remote_analyst_id AS analyst_id,a.action_type,ao.horizon_minutes,ao.status,ao.directional_return
                 FROM quant.analyst_action_intraday_outcomes ao
                 JOIN quant.analyst_trade_actions a ON a.action_id=ao.action_id
                WHERE (a.stated_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                  AND (%s::text IS NULL OR a.remote_analyst_id=%s)""", (start, end, analyst_id, analyst_id)).fetchall()]
        market_days = [dict(row) for row in connection.execute(
            """SELECT DISTINCT ON(exchange_date) exchange_date,market_state,status,concept_positive_ratio,market_amount,
                            amount_change_pct,advancer_ratio
                 FROM quant.market_flow_feature_snapshots
                WHERE exchange_date BETWEEN %s AND %s AND cadence IN ('close','midday')
                ORDER BY exchange_date,CASE cadence WHEN 'close' THEN 0 ELSE 1 END,observed_at DESC""", (start, end)).fetchall()]
        sector_days = [dict(row) for row in connection.execute(
            """SELECT feature.sector_key,sector.label,feature.net_amount,feature.lhb_negative_count,feature.trading_date
                 FROM quant.sector_flow_daily_features feature JOIN quant.sectors sector
                   ON sector.taxonomy_key=feature.taxonomy_key AND sector.sector_key=feature.sector_key
                WHERE feature.taxonomy_key='ths_concept_flow' AND trading_date BETWEEN %s AND %s""", (start, end)).fetchall()]
        theme_board_map = {
            str(row["theme_key"]): str(row["sector_key"])
            for row in connection.execute(
                """SELECT theme_key,sector_key
                     FROM quant.analyst_theme_board_aliases
                    WHERE status='approved' AND taxonomy_key='ths_concept_flow'"""
            ).fetchall()
        }
    result = summarize_evaluation(observations=observations, opinions=opinions, outcomes=outcomes, intraday_outcomes=intraday_outcomes, author_action_outcomes=author_action_outcomes, market_days=market_days, sector_days=sector_days, market_days_for_baseline=market_days, theme_board_map=theme_board_map, start_date=start, end_date=end)
    result["analyst_id"] = analyst_id
    return result


__all__ = ["analyst_market_evaluation", "summarize_evaluation"]
