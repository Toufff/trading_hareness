"""Pure post-close pattern review scoring."""

from __future__ import annotations

from typing import Any, Callable

def review_score(item: dict[str, Any], pattern: dict[str, Any], risk_flags: list[str], *, number: Callable[[Any], float | None]) -> dict[str, Any]:
    """Score only transparent post-close evidence; never treat it as an order signal."""
    daily = item.get("daily_features") or {}
    board = item.get("board_context") or {}
    limit_context = item.get("limit_context") or {}
    lhb = limit_context.get("lhb_context") or {}
    score = 35.0
    reasons = list(limit_context.get("selection_reasons") or [])
    streak = int(limit_context.get("streak_count") or 0)
    score += min(20, streak * 5)
    volume_multiple = float(daily.get("volume_multiple_5d") or 0)
    if volume_multiple >= 2:
        score += 10
    elif volume_multiple >= 1.5:
        score += 6
    board_flow = float(board.get("net_amount") or 0)
    score += 8 if board_flow > 0 else -4 if board.get("exact_member_mapping") else -2
    institution_net = float(lhb.get("institution_net_buy") or 0)
    if institution_net > 0:
        score += 10
    elif institution_net < 0:
        score -= 6
    open_num = number(limit_context.get("open_num"))
    if open_num is not None and open_num <= 2:
        score += 7
        reasons.append("封板稳定")
    elif open_num is not None and open_num > 15:
        score -= 5
        reasons.append("多次开板分歧")
    turnover = float(limit_context.get("turnover_rate") or 0)
    if 5 <= turnover <= 30:
        score += 5
    elif turnover > 40:
        score -= 5
    tags = set(pattern.get("pattern_tags") or [])
    if tags.intersection({"opening_ladder_drive", "opening_drive", "morning_acceleration", "midday_relaunch"}):
        score += 6
        reasons.append("分钟量价点火")
    if "ground_to_sky_reversal" in tags and pattern.get("deep_reversal_impulse"):
        score += 6
        reasons.append("深水反转量价确认")
    score -= min(12, len(set(risk_flags)) * 3)
    score = round(max(0.0, min(100.0, score)), 2)
    tier = "priority_review" if score >= 70 else "candidate_review" if score >= 58 else "research_sample"
    return {"review_score": score, "review_tier": tier, "selection_reasons": list(dict.fromkeys(reasons))}


__all__ = ["review_score"]
