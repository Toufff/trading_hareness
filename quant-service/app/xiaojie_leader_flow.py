"""Research-only quantification of the ``小杰夜报`` leader-flow playbook.

The source material is qualitative.  This module makes each observed rule an
explicit, deterministic feature test over data already retained by the
platform.  It emits a review decision only; it never creates an order or
changes a live threshold.
"""

from __future__ import annotations

from typing import Any, Mapping


MODEL_VERSION = "xiaojie-leader-flow-v1"
INPUT_CONTRACT = "xiaojie-leader-flow-input-v1"

# These are preregistered research defaults, not promoted trading parameters.
# Values which were not stated numerically in the messages remain calibration
# knobs and are returned in ``parameters`` so a walk-forward run can audit them.
DEFAULT_PARAMETERS: dict[str, Any] = {
    "market_gate_min_components": 3,
    # The playbook says "top one or two in the sector", "2-3 days without a new
    # high" and "3-5 day time stop".  Those ranges were previously pinned in
    # code, which put the three most overfit-prone numbers in the module beyond
    # the reach of the walk-forward calibration this file promises.
    "leader_rank_max": 2,
    "days_without_new_high_min": 3,
    "days_without_rise_min": 5,
    "index_volume_ratio_min": 1.0,
    "sector_strength_percentile_min": 0.80,
    "divergence_drawdown_min_pct": 5.0,
    "divergence_drawdown_max_pct": 7.0,
    "limitup_break_rebound_min_pct": 3.0,
    "limitup_break_rebound_max_pct": 5.0,
    "ma5_break_reduce_minutes": 15,
    "ma5_break_reduce_fraction": 0.50,
    "normal_position_fraction": 0.10,
    "leader_position_fraction": 0.20,
    "high_risk_position_fraction": 0.05,
    "high_risk_total_fraction": 0.10,
    "normal_stop_loss_pct": 5.0,
    "swing_stop_loss_min_pct": 8.0,
    "swing_stop_loss_max_pct": 15.0,
    "short_term_stop_loss_min_pct": 10.0,
    "short_term_stop_loss_max_pct": 20.0,
    "icepoint_ma5_distance_min_pct": 3.0,
    "left_side_trial_fraction": 0.05,
    "oversold_rebound_fraction": 0.05,
    "staged_entry_initial_fraction": 0.50,
    "long_term_dca_parts_min": 10,
    "long_term_dca_parts_max": 20,
    "long_term_dca_drawdown_min_pct": 5.0,
    "long_term_dca_drawdown_max_pct": 10.0,
}


#: Exit actions ordered from mildest to strongest.  Any rule that fires may
#: only raise the recommended action, never lower one another rule already set.
EXIT_SEVERITY: dict[str, int] = {
    "hold_or_wait": 0, "reduce_half": 1, "reduce_or_exit": 2, "exit": 3,
}


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _flag(snapshot: Mapping[str, Any], name: str) -> bool:
    """Strict truth test for a snapshot flag.

    Python truthiness is not usable here: a replay whose booleans arrive as
    JSON/CSV strings would read ``"false"`` as true.  That was reachable -
    passing the string ``"false"`` for the one-word-board fields produced a
    high-risk research candidate where a real ``False`` correctly produced
    ``no_trade``.  Anything that is not a genuine bool is treated as absent.
    """
    return _boolean(snapshot.get(name)) is True


def _market_gate(snapshot: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    index_above_support = _boolean(snapshot.get("index_above_support"))
    volume_ratio = _number(snapshot.get("index_volume_ratio"))
    up_count = _number(snapshot.get("breadth_up_count"))
    down_count = _number(snapshot.get("breadth_down_count"))
    main_sector = _boolean(snapshot.get("main_sector_present"))
    components = {
        "index_above_support": index_above_support is True,
        "index_volume_expansion": volume_ratio is not None and volume_ratio >= float(params["index_volume_ratio_min"]),
        "breadth_improving": up_count is not None and down_count is not None and up_count > down_count,
        "main_sector_present": main_sector is True,
    }
    complete = index_above_support is not None and volume_ratio is not None and up_count is not None and down_count is not None and main_sector is not None
    score = sum(components.values())
    return {
        "score": score,
        "minimum": int(params["market_gate_min_components"]),
        "ok": complete and score >= int(params["market_gate_min_components"]),
        "complete": complete,
        "components": components,
        "breadth_ratio": ((up_count - down_count) / (up_count + down_count)) if up_count is not None and down_count is not None and up_count + down_count > 0 else None,
    }


def _mode(snapshot: Mapping[str, Any], params: Mapping[str, Any]) -> str | None:
    if _flag(snapshot, "limit_up_return_flow") and _flag(snapshot, "re_seal_confirmed") and _flag(snapshot, "prior_one_word_board"):
        return "one_word_return_flow"
    if _flag(snapshot, "reverse_wrap_confirmed") and _flag(snapshot, "main_sector_present"):
        return "reverse_wrap"
    drawdown = _number(snapshot.get("drawdown_from_high_pct"))
    rebound = _number(snapshot.get("post_limitup_break_rebound_pct"))
    divergence = (
        drawdown is not None and params["divergence_drawdown_min_pct"] <= abs(drawdown) <= params["divergence_drawdown_max_pct"]
    ) or (
        rebound is not None and params["limitup_break_rebound_min_pct"] <= rebound <= params["limitup_break_rebound_max_pct"]
    )
    if divergence and _flag(snapshot, "support_or_vwap_holds"):
        return "divergence_low_suck"
    if _flag(snapshot, "leader_pullback_to_vwap") and _flag(snapshot, "main_sector_present"):
        return "leader_pullback"
    if _flag(snapshot, "index_right_side_confirmed") and _flag(snapshot, "breakout_confirmed"):
        return "right_side_breakout"
    distance_from_ma5 = _number(snapshot.get("distance_from_ma5_pct"))
    if (_flag(snapshot, "icepoint") and _flag(snapshot, "left_side_signal")
            and distance_from_ma5 is not None
            and distance_from_ma5 >= float(params["icepoint_ma5_distance_min_pct"])):
        return "icepoint_left_trial"
    if _flag(snapshot, "oversold_rebound_confirmed") and _flag(snapshot, "support_or_vwap_holds"):
        return "oversold_rebound"
    if _flag(snapshot, "supplement_candidate") and _flag(snapshot, "leader_not_broken"):
        return "supplement_rotation"
    if _flag(snapshot, "is_etf") and _flag(snapshot, "trend_support_holds"):
        return "etf_trend"
    if _flag(snapshot, "breakout_or_reverse_wrap"):
        return "潜龙出海_swing"
    return None


def evaluate_snapshot(snapshot: Mapping[str, Any], parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate one point-in-time market/candidate snapshot.

    Missing market context fails closed.  The result is deliberately a
    research candidate/watch/no-trade classification with an auditable reason
    list, not a broker instruction.
    """
    supplied = dict(parameters or {})
    unknown = sorted(set(supplied) - set(DEFAULT_PARAMETERS))
    if unknown:
        raise ValueError(f"parameters are not registered: {', '.join(unknown)}")
    params = {**DEFAULT_PARAMETERS, **supplied}
    market = _market_gate(snapshot, params)
    risk_flags: list[str] = []
    reasons: list[str] = []
    if not market["complete"]:
        risk_flags.append("insufficient_market_evidence")
        reasons.append("指数、成交量、宽度和主线板块字段必须完整")
    if not market["ok"]:
        risk_flags.append("market_regime_not_confirmed")
        reasons.append("市场门控未达到预注册最低组件数")
    if _flag(snapshot, "is_back_row"):
        risk_flags.append("back_row_no_chase")
        reasons.append("后排不追高")
    if _flag(snapshot, "futures_stock_both_rising"):
        risk_flags.append("cross_asset_chase_risk")
        reasons.append("期货与股票同步大涨时不追价")

    percentile = _number(snapshot.get("sector_strength_percentile"))
    sector_core = percentile is not None and percentile >= float(params["sector_strength_percentile_min"])
    leader_rank = _number(snapshot.get("candidate_strength_rank"))
    leader_ok = leader_rank is not None and 1 <= leader_rank <= float(params["leader_rank_max"])
    if not sector_core:
        risk_flags.append("sector_core_unconfirmed")
    if not leader_ok:
        risk_flags.append("leader_rank_unconfirmed")

    selected_mode = _mode(snapshot, params)
    high_risk = selected_mode in {"one_word_return_flow", "reverse_wrap"}
    if high_risk:
        risk_flags.append("high_risk_mode")
    if selected_mode is None:
        reasons.append("没有可复现的买点形态")
    elif selected_mode == "one_word_return_flow":
        reasons.append("强板块最强活口完成一字板回流并确认回封")
    elif selected_mode == "reverse_wrap":
        reasons.append("板块同步时出现反包/弱转强")
    elif selected_mode == "divergence_low_suck":
        reasons.append("分歧或急跌后回到支撑/VWAP，满足低吸区间")
    elif selected_mode == "leader_pullback":
        reasons.append("核心龙头回踩分时均价且主线仍在")
    elif selected_mode == "right_side_breakout":
        reasons.append("指数右侧确认且个股放量突破")
    elif selected_mode == "icepoint_left_trial":
        reasons.append("冰点偏离 5 日线达到试错距离，仅允许左侧小仓位")
    elif selected_mode == "oversold_rebound":
        reasons.append("超跌反弹确认且支撑/VWAP 未破")
    elif selected_mode == "supplement_rotation":
        reasons.append("主龙头未破坏，板块补涨候选进入轮动")
    elif selected_mode == "etf_trend":
        reasons.append("ETF/低波动资产趋势支撑有效")
    else:
        reasons.append("潜龙出海突破/反包，仅作波段研究")

    is_etf = _flag(snapshot, "is_etf")
    # A supplement is by definition not the sector leader - the playbook allows
    # it precisely as a follow-on, at a small position.  Gating it on "rank 1-2"
    # made the mode unreachable: the live indicator only marks a name as a
    # supplement candidate at rank 3 or worse, so the two intervals never
    # intersected and the mode could select but never produce a candidate.
    # It is exempted from the leader rank and back-row blocks and stays capped
    # at the small high-risk fraction instead.
    follows_a_leader = selected_mode == "supplement_rotation" and _flag(snapshot, "leader_not_broken")
    leader_gate_ok = leader_ok or is_etf or follows_a_leader
    left_side_without_cushion = selected_mode == "icepoint_left_trial" and (_number(snapshot.get("profit_cushion_pct")) or 0) <= 0
    if left_side_without_cushion:
        risk_flags.append("left_side_without_profit_cushion")
    hard_block = (
        not market["ok"] or not market["complete"]
        or (_flag(snapshot, "is_back_row") and not follows_a_leader)
        or _flag(snapshot, "futures_stock_both_rising") or not sector_core or not leader_gate_ok or selected_mode is None
        or left_side_without_cushion
    )
    decision = "no_trade" if hard_block else "research_candidate"
    if decision == "no_trade":
        position_fraction = 0.0
    elif selected_mode == "icepoint_left_trial":
        position_fraction = float(params["left_side_trial_fraction"])
    elif selected_mode in {"oversold_rebound", "supplement_rotation"}:
        position_fraction = float(params["oversold_rebound_fraction"])
    elif high_risk:
        position_fraction = float(params["high_risk_position_fraction"])
    elif selected_mode == "etf_trend":
        position_fraction = float(params["normal_position_fraction"])
    elif selected_mode == "leader_pullback":
        position_fraction = float(params["leader_position_fraction"])
    else:
        position_fraction = float(params["normal_position_fraction"])

    ma5_duration = _number(snapshot.get("ma5_break_duration_minutes"))
    ma5_recovered = _boolean(snapshot.get("ma5_recovered"))
    days_without_new_high = _number(snapshot.get("days_without_new_high"))
    days_without_rise = _number(snapshot.get("days_without_rise"))
    exit_codes: list[str] = []
    triggered: list[str] = []
    if ma5_duration is not None and ma5_duration >= float(params["ma5_break_reduce_minutes"]) and ma5_recovered is False:
        exit_codes.append("ma5_break_unrecovered")
        triggered.append("reduce_half")
    if _flag(snapshot, "box_support_broken") or _flag(snapshot, "entry_low_broken"):
        exit_codes.append("support_break")
        triggered.append("exit")
    if days_without_new_high is not None and days_without_new_high >= float(params["days_without_new_high_min"]):
        exit_codes.append("no_new_high_3d")
        triggered.append("reduce_or_exit")
    if days_without_rise is not None and days_without_rise >= float(params["days_without_rise_min"]):
        exit_codes.append("time_stop_5d")
        triggered.append("exit")
    if _flag(snapshot, "limit_up_break") and _flag(snapshot, "sector_strength_fades"):
        exit_codes.append("limitup_break_sector_fades")
        triggered.append("reduce_or_exit")
    # Severity is a maximum, never last-write-wins.  Sequential assignment let
    # a milder rule evaluated later overwrite a stronger one: a broken support
    # (exit) that had also gone three days without a new high came back as
    # "reduce_or_exit", so accumulating bearish evidence produced a *weaker*
    # recommendation than any one of those conditions alone.
    exit_action = max(triggered, key=EXIT_SEVERITY.get, default="hold_or_wait")

    stop_loss = {
        "mode": "short_term" if high_risk else ("swing" if selected_mode == "潜龙出海_swing" else "normal"),
        "min_pct": float(params["short_term_stop_loss_min_pct"] if high_risk else (params["swing_stop_loss_min_pct"] if selected_mode == "潜龙出海_swing" else params["normal_stop_loss_pct"])),
        "max_pct": float(params["short_term_stop_loss_max_pct"] if high_risk else (params["swing_stop_loss_max_pct"] if selected_mode == "潜龙出海_swing" else params["normal_stop_loss_pct"])),
    }
    staged_entry = decision != "no_trade" and selected_mode not in {"etf_trend"}
    initial_fraction = round(position_fraction * float(params["staged_entry_initial_fraction"]), 4) if staged_entry else round(position_fraction, 4)
    confirmation_fraction = round(position_fraction - initial_fraction, 4) if staged_entry else 0.0
    return {
        "strategy_key": "xiaojie_leader_flow",
        "model_version": MODEL_VERSION,
        "input_contract": INPUT_CONTRACT,
        "decision": decision,
        "mode": selected_mode,
        "market_gate": market,
        "position": {
            "target_fraction": round(position_fraction, 4),
            "staged_entry": staged_entry,
            "initial_fraction": initial_fraction,
            "confirmation_fraction": confirmation_fraction,
            "high_risk_total_cap_fraction": float(params["high_risk_total_fraction"]),
        },
        "portfolio_policy": {
            "hierarchy": ["risk_management", "market_regime", "style", "sector", "stock", "entry_exit"],
            "allocation": "single_symbol_10_to_20_percent; at_least_two_sectors; reserve_cash",
            "long_term_dca": {
                "parts_min": int(params["long_term_dca_parts_min"]),
                "parts_max": int(params["long_term_dca_parts_max"]),
                "buy_on_drawdown_pct": [float(params["long_term_dca_drawdown_min_pct"]), float(params["long_term_dca_drawdown_max_pct"])],
            },
        },
        "stop_loss": stop_loss,
        "exit": {"action": exit_action, "codes": exit_codes},
        "reasons": reasons,
        "risk_flags": list(dict.fromkeys(risk_flags)),
        "parameters": params,
        "live_effect": "none",
        "boundary": "research_only; point_in_time_snapshot; no_automatic_order",
    }


__all__ = ["DEFAULT_PARAMETERS", "EXIT_SEVERITY", "INPUT_CONTRACT", "MODEL_VERSION", "evaluate_snapshot"]
