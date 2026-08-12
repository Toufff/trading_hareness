"""Offline analyst-language distillation and prompt-variant evaluation.

The module intentionally does not call an LLM.  It persists explicit prompt
contracts and deterministic baseline extractions so a future model provider can
be evaluated against the same point-in-time evidence.  Neither a profile nor a
prompt winner changes a live alert or trade rule.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from psycopg.types.json import Json


SKILL_MODEL_VERSION = "analyst-skill-distillation-v1"
PROMPT_VARIANTS: tuple[dict[str, str], ...] = (
    {
        "key": "strict_action_v1",
        "label": "严格可执行动作",
        "prompt": "仅提取有明确标的、动作、作者原始时刻及价格条件的观点；不补全简称，不把复盘文字变成交易建议。",
    },
    {
        "key": "scenario_context_v1",
        "label": "情景与板块联动",
        "prompt": "提取大盘状态、板块轮动、条件触发与失效条件；个股动作必须与原文证据分开。",
    },
    {
        "key": "risk_first_v1",
        "label": "风险优先复核",
        "prompt": "优先提取减仓、止损、仓位、风险位和否定条件；不把条件性乐观改写为看多。",
    },
)


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _stance(text: str) -> dict[str, int]:
    return {
        "positive": sum(token in text for token in ("看多", "看好", "加仓", "买入", "开仓", "持股", "机会")),
        "negative": sum(token in text for token in ("减仓", "卖出", "出局", "风险", "止损", "谨慎", "回避")),
        "conditional": sum(token in text for token in ("如果", "若", "等", "回踩", "低开", "不破", "突破")),
    }


def _variant_payload(variant: dict[str, str], reports: list[dict[str, Any]], actions: list[dict[str, Any]]) -> dict[str, Any]:
    """A deterministic baseline corresponding to an immutable prompt contract."""
    action_counts = Counter(str(action["action_type"]) for action in actions)
    if variant["key"] == "strict_action_v1":
        selected = [action for action in actions if action.get("symbol") and action.get("action_type") not in {"watch"}]
        quality = "exact_alias_and_author_time_only"
    elif variant["key"] == "risk_first_v1":
        selected = [action for action in actions if action.get("action_type") in {"reduce", "watch"}]
        quality = "risk_action_subset"
    else:
        selected = actions
        quality = "all_review_actions_plus_market_context"
    return {
        "variant_key": variant["key"], "label": variant["label"], "prompt": variant["prompt"],
        "baseline": "deterministic_text_only", "coverage": {
            "reports": len(reports), "actions": len(selected), "action_mix": dict(action_counts),
            "explicit_price_conditions": sum(action.get("target_price") is not None for action in selected),
            "author_timestamps": sum(action.get("stated_at") is not None for action in selected),
        },
        "evaluation_status": "collecting_point_in_time_outcomes",
        "quality_boundary": quality,
        "promotion": "disabled_requires_200_mature_actions_60_trading_days_and_manual_approval",
    }


def rebuild_analyst_skill_profile(connection: Any, analyst_id: str, as_of_date: date) -> dict[str, Any]:
    """Materialize a research-only language skill card for one analyst."""
    reports = [dict(row) for row in connection.execute(
        """SELECT remote_report_id,report_date,summary,sections,remote_updated_at,remote_created_at,synced_at
             FROM quant.remote_reports WHERE remote_analyst_id=%s AND report_date<=%s
             ORDER BY report_date DESC,remote_updated_at DESC LIMIT 60""",
        (analyst_id, as_of_date),
    ).fetchall()]
    actions = [dict(row) for row in connection.execute(
        """SELECT action_id,remote_report_id,symbol,label,action_type,direction,stated_at,available_at,target_price,evidence,raw
             FROM quant.analyst_trade_actions WHERE remote_analyst_id=%s
               AND (stated_at AT TIME ZONE 'Asia/Shanghai')::date<=%s
             ORDER BY stated_at DESC LIMIT 500""",
        (analyst_id, as_of_date),
    ).fetchall()]
    text = "\n".join(
        "\n".join(str(value) for value in (report.get("summary"), *(dict(report.get("sections") or {}).values())) if value)
        for report in reports
    )
    stance = _stance(text)
    action_counts = Counter(str(action["action_type"]) for action in actions)
    action_dates = {action["stated_at"].date() for action in actions if action.get("stated_at")}
    # A report compiled after the source timestamp is excellent for replay but
    # unavailable to a contemporaneous system.  Count it explicitly rather
    # than letting it leak into an apparent online learning sample.
    factor_eligible = [action for action in actions if action.get("available_at") and action.get("stated_at") and action["available_at"] <= action["stated_at"]]
    profile = {
        "model_version": SKILL_MODEL_VERSION,
        "analyst_id": analyst_id,
        "as_of_date": str(as_of_date),
        "mode": "offline_language_distillation_research_only",
        "language_style": {
            "report_count": len(reports), "stance_tokens": stance,
            "conditionality_ratio": round(stance["conditional"] / max(1, stance["positive"] + stance["negative"] + stance["conditional"]), 4),
            "action_mix": dict(action_counts), "unique_action_symbols": len({action["symbol"] for action in actions}),
            "author_timed_actions": len(actions), "author_timed_days": len(action_dates),
            "explicit_price_conditions": sum(action.get("target_price") is not None for action in actions),
        },
        "point_in_time_integrity": {
            "factor_eligible_actions": len(factor_eligible), "replay_only_actions": len(actions) - len(factor_eligible),
            "rule": "author-stated timestamps are not factor eligible unless the system received the message no later than that timestamp",
        },
        "skill_score": {
            "status": "insufficient_mature_point_in_time_samples",
            "reason": "language profile is descriptive until 200 mature actions across 60 trading days",
            "mature_actions": 0, "required_actions": 200, "trading_days": len(action_dates), "required_trading_days": 60,
        },
        "prompt_lab": [_variant_payload(variant, reports, actions) for variant in PROMPT_VARIANTS],
    }
    connection.execute(
        """INSERT INTO quant.analyst_skill_profiles(remote_analyst_id,as_of_date,model_version,status,profile)
           VALUES(%s,%s,%s,'collecting',%s)
           ON CONFLICT(remote_analyst_id,as_of_date,model_version) DO UPDATE SET status=EXCLUDED.status,profile=EXCLUDED.profile,updated_at=now()""",
        (analyst_id, as_of_date, SKILL_MODEL_VERSION, Json(profile)),
    )
    return profile


def rebuild_all_analyst_skill_profiles(connection: Any, as_of_date: date) -> dict[str, Any]:
    analysts = [str(row["remote_analyst_id"]) for row in connection.execute(
        "SELECT remote_analyst_id FROM quant.remote_analysts ORDER BY remote_analyst_id"
    ).fetchall()]
    profiles = [rebuild_analyst_skill_profile(connection, analyst_id, as_of_date) for analyst_id in analysts]
    return {"model_version": SKILL_MODEL_VERSION, "as_of_date": str(as_of_date), "profiles": profiles,
            "policy_update": "disabled"}


def analyst_skill_profiles(database: Any, analyst_id: str | None, limit: int) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    with database.transaction() as connection:
        if analyst_id:
            rows = connection.execute(
                """SELECT remote_analyst_id,as_of_date,model_version,status,profile,created_at,updated_at
                     FROM quant.analyst_skill_profiles WHERE remote_analyst_id=%s
                     ORDER BY as_of_date DESC LIMIT %s""", (analyst_id, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT DISTINCT ON(remote_analyst_id) remote_analyst_id,as_of_date,model_version,status,profile,created_at,updated_at
                     FROM quant.analyst_skill_profiles ORDER BY remote_analyst_id,as_of_date DESC LIMIT %s""", (limit,),
            ).fetchall()
    return {"items": [dict(row) for row in rows], "model_version": SKILL_MODEL_VERSION,
            "data_boundary": "offline text-only distillation; profiles and prompt variants cannot change live rules"}
