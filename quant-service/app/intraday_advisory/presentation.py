"""Human-facing vocabulary and fail-closed readability checks for Feishu."""

from __future__ import annotations

import re
from typing import Any


STATE_TEXT = {"calm": "整体平稳", "watch": "需要关注", "risk": "风险升高"}
ROLE_TEXT = {
    "holding": "当前持仓", "recommendation": "推荐候选",
    "market_index": "市场指数", "sector": "行业板块",
}
TOKEN_TEXT = {
    "breakout_hold": "突破后的承接验证阶段",
    "new_buy": "推荐候选",
    "buy_authorized=false": "当前尚未满足买入条件",
    "quote=null": "实时行情暂未取得",
    "MARKET.INDEX": "核心市场指数",
}
_RAW_TOKEN = re.compile(
    r"\b(?:market_state|attention_symbols|position_weight_pct|sellable_quantity|"
    r"amount_ratio|active_ratio|buy_authorized|breakout_hold|new_buy|quote=null|"
    r"MARKET\.INDEX)\b|\b(?:true|false|null)\b|\b[a-z][a-z0-9]*_[a-z0-9_]+\b|"
    r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b(?:intraday-advisory|quant-research|trading-hareness)\b",
    re.IGNORECASE,
)


def symbol_text(symbol: str, name: str | None = None) -> str:
    code = str(symbol or "").split(".", 1)[0]
    return f"{name}（{code}）" if name else code


def state_text(value: Any) -> str:
    return STATE_TEXT.get(str(value or "watch"), "需要关注")


def role_text(value: str) -> str:
    return ROLE_TEXT.get(value, "观察对象")


def amount_text(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "数据暂缺"
    if abs(amount) >= 100_000_000:
        return f"{amount / 100_000_000:.2f}亿元"
    if abs(amount) >= 10_000:
        return f"{amount / 10_000:.1f}万元"
    return f"{amount:.0f}元"


def metric_lines(metrics: dict[str, Any]) -> list[str]:
    """Translate only known evidence fields; unknown internals never leak."""
    rows: list[str] = []
    if metrics.get("price") is not None:
        rows.append(f"最新价 {_number(metrics.get('price'))} 元")
    if metrics.get("pre_close_change_pct") is not None:
        rows.append(f"较昨收 {_signed_pct(metrics.get('pre_close_change_pct'))}")
    if metrics.get("price_change_pct") is not None:
        seconds = int(float(metrics.get("window_seconds") or 60))
        rows.append(f"近{max(1, seconds // 60)}分钟 {_signed_pct(metrics.get('price_change_pct'))}")
    if metrics.get("amount_ratio") is not None:
        rows.append(f"1分钟成交额约为近期基线的 {_number(metrics.get('amount_ratio'), 1)} 倍")
    if metrics.get("amount_delta") is not None:
        rows.append(f"近1分钟成交额 {amount_text(metrics.get('amount_delta'))}")
    if metrics.get("active_ratio") is not None:
        ratio = float(metrics["active_ratio"])
        direction = "外盘增量占优" if ratio >= 0 else "内盘增量占优"
        rows.append(f"近1分钟{direction}，内外盘差约占成交量 {abs(ratio) * 100:.1f}%")
    return rows or ["有效行情证据暂缺"]


def humanize_text(value: Any) -> str:
    text = str(value or "").strip()
    for raw, readable in TOKEN_TEXT.items():
        text = text.replace(raw, readable)
    text = re.sub(r"\b(\d{6})\.(?:SH|SZ|BJ)\b", r"\1", text, flags=re.IGNORECASE)
    return text


def ensure_readable_card(card: dict[str, Any]) -> None:
    # Structural card JSON legitimately contains booleans such as
    # ``wide_screen_mode: true``.  Only user-visible string values are gated.
    visible = "\n".join(_visible_content(card))
    match = _RAW_TOKEN.search(visible)
    if match:
        raise ValueError(f"untranslated_internal_token:{match.group(0)}")


def _visible_content(value: Any) -> list[str]:
    if isinstance(value, dict):
        own = [str(value["content"])] if isinstance(value.get("content"), str) else []
        return own + [item for key, child in value.items() if key != "content"
                      for item in _visible_content(child)]
    if isinstance(value, (list, tuple)):
        return [item for child in value for item in _visible_content(child)]
    return []


def _number(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "数据暂缺"


def _signed_pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "数据暂缺"
    return f"{number:+.2f}%"


__all__ = [
    "amount_text", "ensure_readable_card", "humanize_text", "metric_lines",
    "role_text", "state_text", "symbol_text",
]
