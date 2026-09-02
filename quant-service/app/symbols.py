"""Single source of truth for normalizing A-share instrument symbols.

Before this module existed, at least five call sites each carried their own
ad-hoc symbol inference (taking the trailing 6 digits of whatever string
arrived, or guessing the exchange from a single leading digit).  Two concrete
mis-classifications were confirmed in the audit: ``sh000300`` (CSI 300, a
Shanghai index) was turned into ``000300.SZ`` (a nonexistent Shenzhen stock)
by code that blindly took the last 6 digits and routed "0" prefixes to SZ;
and a bare "9"-leading code was routed to the Beijing Stock Exchange
regardless of whether it was actually a Shanghai B-share (``900xxx``) or a
genuine BSE listing (``920xxx``, the exchange's new-format prefix).

``canonical_symbol`` is deliberately pure (no I/O, no database) so every
caller -- ingestion, replay, and live scanning alike -- normalizes the exact
same way.  A bare 6-digit code is ambiguous between an index and a stock
(``000001`` is both the Shanghai Composite Index and Ping An Bank), so
callers must say which they mean via ``kind``; an explicit ``sh``/``sz``/
``bj`` prefix or ``.SH``/``.SZ``/``.BJ`` suffix already carries the exchange
and does not need ``kind`` to resolve.
"""

from __future__ import annotations

import re
from typing import Literal

SymbolKind = Literal["stock", "index", "any"]

_SUFFIX_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")
_PREFIX_RE = re.compile(r"^(SH|SZ|BJ)(\d{6})$")
_BARE_RE = re.compile(r"^\d{6}$")


def _board_for_index(code: str) -> str | None:
    """SSE indices are 000xxx (e.g. 000001 上证指数, 000300 沪深300,
    000688 科创50); SZSE indices are 399xxx (e.g. 399001 深证成指,
    399006 创业板指)."""
    if code.startswith("000"):
        return "SH"
    if code.startswith("399"):
        return "SZ"
    return None


def _board_for_stock(code: str) -> str | None:
    """Board inference for a bare stock code, most specific prefix first.

    ``92`` (the exchange's new-format Beijing prefix) must be checked before
    the legacy NEEQ ``9``-adjacent ``90`` Shanghai B-share prefix, and both
    must be checked before the generic ``4``/``8`` legacy Beijing prefixes.
    """
    if code.startswith("90"):
        return "SH"  # 沪B (Shanghai B-share)
    if code.startswith("20"):
        return "SZ"  # 深B (Shenzhen B-share)
    if code.startswith("92") or code.startswith(("4", "8")):
        return "BJ"  # 北交所: new 92xxxx format plus legacy NEEQ 4/8xxxxx
    if code.startswith(("60", "68")):
        return "SH"
    if code.startswith(("00", "30")):
        return "SZ"
    return None


def canonical_symbol(value: object, kind: SymbolKind = "stock") -> str | None:
    """Return ``"<6-digit code>.<SH|SZ|BJ>"`` from any A-share symbol dressing.

    Accepts an ``sh``/``sz``/``bj`` prefix, a ``.SH``/``.SZ``/``.BJ`` suffix,
    or a bare 6-digit code (case-insensitive).  Returns ``None`` for anything
    it cannot confidently resolve rather than guessing.
    """
    if value is None:
        return None
    raw = str(value).strip().upper()
    if not raw:
        return None

    suffix_match = _SUFFIX_RE.match(raw)
    if suffix_match:
        code, board = suffix_match.group(1), suffix_match.group(2)
        return f"{code}.{board}"

    prefix_match = _PREFIX_RE.match(raw)
    if prefix_match:
        board, code = prefix_match.group(1), prefix_match.group(2)
        return f"{code}.{board}"

    if _BARE_RE.match(raw):
        board = None
        if kind in ("index", "any"):
            board = _board_for_index(raw)
        if board is None and kind in ("stock", "any"):
            board = _board_for_stock(raw)
        return f"{raw}.{board}" if board else None

    return None


__all__ = ["SymbolKind", "canonical_symbol"]
