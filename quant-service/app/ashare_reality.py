"""Shared A-share tradability and paper-cost reality model.

The module is intentionally pure and broker-free.  Live policy may use its
reason codes to explain an alert, paper execution uses it to reject a
non-fill, and a future replay can use the exact same contract.  It does not
claim queue position at a price limit or submit an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

from .market_rules import a_share_limit_ratio


LOT_SIZE = 100
DEFAULT_COMMISSION_RATE = Decimal("0.0003")
DEFAULT_STAMP_TAX_RATE = Decimal("0.001")
DEFAULT_SLIPPAGE_BPS = Decimal("5")


@dataclass(frozen=True)
class AshareTradability:
    allowed: bool
    reasons: tuple[str, ...] = ()


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def round_board_lot(quantity: int | float | Decimal, lot_size: int = LOT_SIZE) -> int:
    """Round a stock order down to the exchange board-lot boundary."""
    value = Decimal(str(quantity or 0))
    return int((value / lot_size).to_integral_value(rounding=ROUND_DOWN) * lot_size)


def price_limit_state(*, symbol: str | None, quote: dict[str, Any] | None) -> dict[str, bool | float | None]:
    """Resolve limit state from exact prices first, then market-rule fallback.

    Public quote feeds often expose a percentage but not the exchange limit
    price.  The fallback therefore uses the correct board/ST ratio and a
    small tolerance; it is deliberately a non-fill safeguard, not a claim
    that an order could have joined a limit queue.
    """
    quote = quote or {}
    raw = quote.get("raw") if isinstance(quote.get("raw"), dict) else {}
    merged = {**raw, **quote}
    resolved_symbol = str(symbol or merged.get("symbol") or merged.get("ts_code") or "")
    is_st = bool(merged.get("is_st"))
    price = _number(merged.get("price"))
    limit_up = _number(merged.get("limit_up"))
    limit_down = _number(merged.get("limit_down"))
    at_limit_up = bool(merged.get("at_limit_up"))
    at_limit_down = bool(merged.get("at_limit_down"))
    if price is not None and limit_up is not None:
        at_limit_up = at_limit_up or price >= limit_up * 0.999
    if price is not None and limit_down is not None:
        at_limit_down = at_limit_down or price <= limit_down * 1.001
    # Percentage is only an evidence fallback.  Use 98% of the applicable
    # band to retain the prior conservative main-board 9.8% behavior while
    # correctly handling 20%, 30%, and ST 5% price bands.
    pct_change = _number(merged.get("pct_change"))
    ratio = a_share_limit_ratio(resolved_symbol, is_st=is_st)
    threshold_pct = ratio * 100.0 * 0.98
    if pct_change is not None and limit_up is None:
        at_limit_up = at_limit_up or pct_change >= threshold_pct
    if pct_change is not None and limit_down is None:
        at_limit_down = at_limit_down or pct_change <= -threshold_pct
    return {
        "at_limit_up": at_limit_up,
        "at_limit_down": at_limit_down,
        "limit_ratio": ratio,
        "limit_up": limit_up,
        "limit_down": limit_down,
    }


def assess_tradability(*, side: str, requested_quantity: int, quote: dict[str, Any] | None,
                        position: dict[str, Any] | None = None, symbol: str | None = None) -> AshareTradability:
    """Apply T+1, suspension, limit, and quantity constraints deterministically."""
    normalized_side = str(side or "").lower()
    quote = quote or {}
    reasons: list[str] = []
    if normalized_side not in {"buy", "sell"}:
        reasons.append("unsupported_order_side")
    if requested_quantity <= 0:
        reasons.append("non_positive_quantity")
    if quote.get("is_suspended"):
        reasons.append("suspended")
    if normalized_side == "sell" and int((position or {}).get("sellable_quantity") or 0) < requested_quantity:
        reasons.append("t_plus_one_or_insufficient_sellable_quantity")
    limits = price_limit_state(symbol=symbol, quote=quote)
    if normalized_side == "buy" and bool(limits["at_limit_up"]) and not quote.get("allow_limit_fill"):
        reasons.append("limit_up_non_fill_risk")
    if normalized_side == "sell" and bool(limits["at_limit_down"]) and not quote.get("allow_limit_fill"):
        reasons.append("limit_down_non_fill_risk")
    return AshareTradability(not reasons, tuple(dict.fromkeys(reasons)))


def estimate_trade_cost(*, side: str, quantity: int, price: Decimal | float,
                        commission_rate: Decimal = DEFAULT_COMMISSION_RATE,
                        stamp_tax_rate: Decimal = DEFAULT_STAMP_TAX_RATE,
                        slippage_bps: Decimal = DEFAULT_SLIPPAGE_BPS) -> dict[str, Decimal]:
    """Estimate A-share paper costs; sell-side stamp tax only."""
    notional = Decimal(str(price)) * Decimal(max(0, quantity))
    commission = max(Decimal("5"), notional * commission_rate) if notional else Decimal("0")
    stamp = notional * stamp_tax_rate if str(side).lower() == "sell" else Decimal("0")
    slippage = notional * slippage_bps / Decimal("10000")
    return {
        "notional": notional,
        "commission": commission,
        "stamp_tax": stamp,
        "slippage": slippage,
        "total_cost": commission + stamp + slippage,
    }


def round_trip_cost_pct(*, commission_rate: Decimal = DEFAULT_COMMISSION_RATE,
                        stamp_tax_rate: Decimal = DEFAULT_STAMP_TAX_RATE,
                        slippage_bps: Decimal = DEFAULT_SLIPPAGE_BPS) -> Decimal:
    """One buy plus one sell, as a percentage of notional.

    Research settles in percentage returns and has no position size, so it
    cannot call ``estimate_trade_cost``; deriving the round trip from the same
    constants keeps one source of truth rather than a second rate table that
    drifts from the paper-trading one.

    The commission floor is deliberately absent: it depends on notional, and a
    percentage that silently assumed one would be wrong at every other size.
    Costs are therefore understated for small positions, which is the
    direction that flatters a strategy - read a marginal net edge accordingly.
    """
    buy = commission_rate + slippage_bps / Decimal("10000")
    sell = commission_rate + stamp_tax_rate + slippage_bps / Decimal("10000")
    return (buy + sell) * Decimal("100")


__all__ = [
    "AshareTradability", "DEFAULT_COMMISSION_RATE", "DEFAULT_SLIPPAGE_BPS", "DEFAULT_STAMP_TAX_RATE",
    "LOT_SIZE", "assess_tradability", "estimate_trade_cost", "price_limit_state", "round_board_lot",
    "round_trip_cost_pct",
]
