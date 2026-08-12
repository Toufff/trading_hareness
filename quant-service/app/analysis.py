from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


SYMBOL_PATTERN = re.compile(r"(?<!\d)(?:60\d{4}|68\d{4}|00\d{4}|30\d{4}|8\d{5}|4\d{5})(?!\d)")
POSITIVE_TERMS = ("看多", "看好", "买入", "加仓", "布局", "低吸", "机会", "推荐", "上行", "增持")
NEGATIVE_TERMS = ("看空", "回避", "减仓", "卖出", "避雷", "风险", "下跌", "止损", "谨慎")
# These are explicit portfolio actions used by the analyst feed.  They are
# intentionally kept separate from generic sentiment words: mentioning a
# company alongside "机会" is weaker than an actual "调入" or "调出" order.
EXPLICIT_POSITIVE_ACTIONS = ("调入", "买入", "加仓", "增持", "低吸", "建仓", "布局")
EXPLICIT_NEGATIVE_ACTIONS = ("调出", "卖出", "减仓", "清仓", "离场", "止损", "避雷")
HORIZONS = (("超短", 1), ("短线", 5), ("本周", 5), ("中线", 20), ("一个月", 20), ("长期", 60), ("长线", 60))
EXTRACTOR_VERSION = "rules-v2"


def normalize_symbol(code: str) -> tuple[str, str]:
    if code.startswith(("60", "68")):
        return f"{code}.SH", "SSE"
    if code.startswith(("00", "30")):
        return f"{code}.SZ", "SZSE"
    return f"{code}.BJ", "BSE"


def choose_horizon(text: str) -> int:
    for term, days in HORIZONS:
        if term in text:
            return days
    return 20


def classify_direction(context: str) -> tuple[int, float, float]:
    explicit_positive = sum(term in context for term in EXPLICIT_POSITIVE_ACTIONS)
    explicit_negative = sum(term in context for term in EXPLICIT_NEGATIVE_ACTIONS)
    if explicit_positive > 0 and explicit_negative == 0:
        return 1, min(0.95, 0.70 + explicit_positive * 0.08), min(0.95, 0.78 + explicit_positive * 0.06)
    if explicit_negative > 0 and explicit_positive == 0:
        return -1, min(0.95, 0.70 + explicit_negative * 0.08), min(0.95, 0.78 + explicit_negative * 0.06)
    # Mixed explicit actions (for example, a rebalance paragraph) remain
    # neutral unless the local context contains a clear single-stock action.
    positive = sum(term in context for term in POSITIVE_TERMS)
    negative = sum(term in context for term in NEGATIVE_TERMS)
    if positive > negative:
        return 1, min(0.95, 0.50 + positive * 0.12), min(0.90, 0.55 + positive * 0.10)
    if negative > positive:
        return -1, min(0.95, 0.50 + negative * 0.12), min(0.90, 0.55 + negative * 0.10)
    return 0, 0.50, 0.35


def direction_source(context: str) -> str:
    """Explain whether a direction came from an explicit action or weak text."""
    positive = any(term in context for term in EXPLICIT_POSITIVE_ACTIONS)
    negative = any(term in context for term in EXPLICIT_NEGATIVE_ACTIONS)
    if positive and not negative:
        return "explicit_action_positive"
    if negative and not positive:
        return "explicit_action_negative"
    lexical_positive = any(term in context for term in POSITIVE_TERMS)
    lexical_negative = any(term in context for term in NEGATIVE_TERMS)
    if lexical_positive or lexical_negative:
        return "lexical_context"
    return "neutral"


@dataclass(frozen=True)
class ExtractedSignal:
    symbol: str
    exchange: str
    direction: int
    strength: float
    horizon_days: int
    evidence_text: str
    evidence_offset: int
    extraction_confidence: float


def extract_signals(text: str) -> list[ExtractedSignal]:
    found: dict[str, ExtractedSignal] = {}
    for match in SYMBOL_PATTERN.finditer(text):
        symbol, exchange = normalize_symbol(match.group(0))
        # Opinions for multiple securities are often separated by a Chinese
        # semicolon or full stop. Keep classification inside that sentence so a
        # nearby “回避 B” cannot neutralise “看好 A”.
        boundaries = [text.rfind(marker, 0, match.start()) for marker in "。！？；\n"]
        start = max(boundaries) + 1
        endings = [text.find(marker, match.end()) for marker in "。！？；\n"]
        end = min((offset for offset in endings if offset >= 0), default=len(text))
        if end - start < 8:
            start = max(0, match.start() - 80)
            end = min(len(text), match.end() + 120)
        context = text[start:end]
        direction, strength, confidence = classify_direction(context)
        evidence = context.strip()[:300]
        signal = ExtractedSignal(
            symbol=symbol,
            exchange=exchange,
            direction=direction,
            strength=strength,
            horizon_days=choose_horizon(context),
            evidence_text=evidence or match.group(0),
            evidence_offset=match.start(),
            extraction_confidence=confidence,
        )
        # A directional mention is more informative than an earlier neutral one.
        if symbol not in found or abs(signal.direction) > abs(found[symbol].direction):
            found[symbol] = signal
    return list(found.values())


def keywords(text: str) -> list[str]:
    words = re.findall(r"[\u4e00-\u9fff]{2,8}", text)
    ignored = {"今天", "我们", "市场", "分析", "这个", "可以", "还是", "已经", "就是"}
    result: list[str] = []
    for word in words:
        if word in ignored or word in result:
            continue
        result.append(word)
        if len(result) == 30:
            break
    return result


def as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)
