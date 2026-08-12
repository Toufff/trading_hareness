"""Deterministic, text-only reconstruction of timestamped analyst actions.

These records are deliberately separate from :mod:`analyst_claims`.  A chat
archive can preserve an author-stated intraday timestamp even when our service
only receives the compiled report after the close.  That timestamp is useful
for review, but it must never be mistaken for the system's point-in-time
availability when calculating live strategy factors.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Json


ANQIANG_ANALYST_ID = "anqiang-touzi-riji"
_CN = ZoneInfo("Asia/Shanghai")

# Short aliases are intentionally a small reviewed list.  Do not resolve a
# generic name such as "中船" or "长光": a wrong ticker is worse than an
# unresolved action in a research ledger.
ANQIANG_SYMBOL_ALIASES: tuple[tuple[str, str, str], ...] = (
    ("国际复材", "301526.SZ", "国际复材"), ("复材", "301526.SZ", "国际复材"),
    ("国瓷材料", "300285.SZ", "国瓷材料"), ("国瓷材C", "300285.SZ", "国瓷材料"), ("国瓷", "300285.SZ", "国瓷材料"),
    ("宏景KJ", "301396.SZ", "宏景科技"), ("宏景", "301396.SZ", "宏景科技"),
    ("长川", "300604.SZ", "长川科技"), ("云南锗", "002428.SZ", "云南锗业"), ("云锗", "002428.SZ", "云南锗业"),
    ("江丰", "300666.SZ", "江丰电子"), ("申菱", "301018.SZ", "申菱环境"),
    ("天孚", "300394.SZ", "天孚通信"), ("仕佳", "688313.SH", "仕佳光子"),
    ("致尚", "301486.SZ", "致尚科技"), ("南亚", "688519.SH", "南亚新材"),
    ("红板", "603459.SH", "红板科技"), ("中船特气", "688146.SH", "中船特气"),
    ("中船特Q", "688146.SH", "中船特气"), ("新锐", "688257.SH", "新锐股份"),
    ("德业", "605117.SH", "德业股份"), ("天华", "300390.SZ", "天华新能"),
    ("卓胜", "300782.SZ", "卓胜微"),
)

_HEADING = re.compile(r"^###\s+(?P<hour>\d{2}):(?P<minute>\d{2})\s+〈[^〉]+〉\s*$", re.M)
_PRICE = re.compile(r"(?<!\d)(\d{1,4}(?:\.\d{1,2})?)(?:左右)?(?:做差价|减仓|出局|高抛|接回|加仓)")
_BARE_PRICE = re.compile(r"(?<!\d)(\d{1,4}(?:\.\d{1,2})?)(?:左右)?\s*$")
_ACTION_PATTERNS: tuple[tuple[str, int, re.Pattern[str]], ...] = (
    ("add_t", 1, re.compile(r"(?:加仓做T|加仓.*做T|低点加仓)")),
    ("buy", 1, re.compile(r"(?:开新仓|开仓|进场|接回)")),
    ("hold", 1, re.compile(r"(?:持股|继续持|底仓持)")),
    ("reduce", -1, re.compile(r"(?:减仓|高抛|出局|做T出)")),
    ("trade", 0, re.compile(r"(?:做差价|做T)")),
    ("watch", 0, re.compile(r"关注")),
)


def _operation(text: str) -> tuple[str, int] | None:
    """Return the last stated local action, never an inferred order or fill.

    A phrase such as ``上午减仓的资金现在接回`` is a new ``buy`` action,
    not a sell.  Taking the last explicit verb matches its grammatical intent
    and prevents a prior action in the same clause from winning by priority.
    """
    matches = [
        (match.start(), action_type, direction)
        for action_type, direction, pattern in _ACTION_PATTERNS
        for match in pattern.finditer(text)
    ]
    if not matches:
        return None
    # Compound verbs are ordered by semantic specificity, not their character
    # offset: ``加仓做T`` is an add-with-T action rather than a neutral trade.
    if any(action_type == "add_t" for _, action_type, _ in matches):
        return ("add_t", 1)
    # Re-entry must win a historical reduce in ``上午减仓的资金现在接回``.
    if any(action_type == "buy" for _, action_type, _ in matches):
        return ("buy", 1)
    _, action_type, direction = max(matches, key=lambda item: item[0])
    return action_type, direction


def _aliases(text: str) -> list[tuple[str, str, str]]:
    """Return longest reviewed aliases once, avoiding ``国瓷`` inside ``国瓷材料``."""
    found: list[tuple[int, str, str, str]] = []
    occupied: list[tuple[int, int]] = []
    for alias, symbol, label in sorted(ANQIANG_SYMBOL_ALIASES, key=lambda item: len(item[0]), reverse=True):
        for match in re.finditer(re.escape(alias), text):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            occupied.append((match.start(), match.end()))
            found.append((match.start(), alias, symbol, label))
    return [(alias, symbol, label) for _, alias, symbol, label in sorted(found)]


def _sections(raw_content: str, report_date: date) -> list[tuple[datetime, str, str]]:
    """Split archive material into author-stated time blocks."""
    matches = list(_HEADING.finditer(raw_content))
    result: list[tuple[datetime, str, str]] = []
    for index, match in enumerate(matches):
        body = raw_content[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(raw_content)].strip()
        if not body:
            continue
        stated_at = datetime.combine(
            report_date, time(int(match.group("hour")), int(match.group("minute"))), tzinfo=_CN,
        )
        result.append((stated_at, match.group(0), body))
    return result


def parse_anqiang_trade_actions(report_date: date, raw_content: str, *, available_at: datetime) -> list[dict[str, Any]]:
    """Extract reviewed aliases/actions from one report's timestamped prose.

    ``stated_at`` is supplied by the archive body.  ``available_at`` is always
    preserved independently so callers can enforce point-in-time research
    rules.  The parser does not consume remote media or URLs.
    """
    actions: list[dict[str, Any]] = []
    for stated_at, heading, body in _sections(raw_content, report_date):
        for sentence in re.split(r"[。；;！!？?\n]+", body):
            sentence = " ".join(sentence.split()).strip()
            if not sentence or "不做实盘买入依据" in sentence:
                continue
            pending: list[tuple[str, str, str, float | None, str]] = []
            # Comma-separated lists are common in the source.  An action at
            # the end (``长川、江丰、申菱等持股``) applies to the preceding
            # unqualified aliases in that same sentence only.
            for fragment in (item.strip() for item in re.split(r"[，,、]", sentence) if item.strip()):
                aliases = _aliases(fragment)
                operation = _operation(fragment)
                price_match = _PRICE.search(fragment) or _BARE_PRICE.search(fragment)
                target_price = float(price_match.group(1)) if price_match else None
                if operation is None:
                    pending.extend((alias, symbol, label, target_price, fragment) for alias, symbol, label in aliases)
                    continue
                action_type, direction = operation
                targets = [*pending, *( (alias, symbol, label, target_price, fragment) for alias, symbol, label in aliases )]
                pending = []
                for alias, symbol, label, local_target_price, evidence in targets:
                    fingerprint = hashlib.sha256(
                        f"{report_date}|{stated_at.isoformat()}|{symbol}|{action_type}|{evidence}".encode("utf-8")
                    ).hexdigest()
                    actions.append({
                        "symbol": symbol, "label": label, "action_type": action_type, "direction": direction,
                        "stated_at": stated_at, "available_at": available_at, "target_price": local_target_price,
                        "evidence": evidence, "raw": {"alias": alias, "heading": heading, "timing_status": "author_stated_unverified"},
                        "content_sha256": fingerprint,
                    })
    return actions


def sync_anqiang_trade_actions(connection: Any, report: dict[str, Any], *, available_at: datetime) -> int:
    """Replace one report's reconstructed actions atomically and idempotently."""
    analyst = report.get("analyst") if isinstance(report.get("analyst"), dict) else {}
    analyst_id = str(analyst.get("analyst_id") or report.get("analyst_id") or "")
    if analyst_id != ANQIANG_ANALYST_ID:
        return 0
    report_id = str(report.get("report_id") or "")
    report_date = date.fromisoformat(str(report.get("date")))
    sections = report.get("sections") if isinstance(report.get("sections"), dict) else {}
    raw_content = str(sections.get("raw_content") or "")
    actions = parse_anqiang_trade_actions(report_date, raw_content, available_at=available_at)
    connection.execute("DELETE FROM quant.analyst_trade_actions WHERE remote_report_id=%s", (report_id,))
    for action in actions:
        connection.execute(
            """INSERT INTO quant.analyst_trade_actions(
                   remote_report_id,remote_analyst_id,symbol,label,action_type,direction,stated_at,available_at,target_price,
                   evidence,raw,content_sha256)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(remote_report_id,content_sha256) DO NOTHING""",
            (report_id, analyst_id, action["symbol"], action["label"], action["action_type"], action["direction"],
             action["stated_at"], action["available_at"], action["target_price"], action["evidence"],
             Json(action["raw"]), action["content_sha256"]),
        )
    return len(actions)
