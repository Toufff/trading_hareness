"""Pure A-share paper matching and ledger rules for the agent account.

Fills walk the live five-level book instead of assuming the last price, so a
market order pays the spread and a large order moves through depth.  Nothing
here touches a database, a provider or a broker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..ashare_reality import LOT_SIZE, estimate_trade_cost
from ..market_rules import a_share_limit_ratio, is_st_security_name

SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
CENT = Decimal("0.01")
MAX_ORDERS_PER_DECISION = 10
MAX_FOCUS_SYMBOLS = 15


def dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal("0")
    except Exception:  # noqa: BLE001 - malformed provider numbers count as zero
        return Decimal("0")


def limit_prices(symbol: str, name: str | None, pre_close: Any) -> tuple[Decimal | None, Decimal | None]:
    base = dec(pre_close)
    if base <= 0:
        return None, None
    ratio = Decimal(str(a_share_limit_ratio(symbol, is_st=is_st_security_name(name))))
    up = (base * (1 + ratio)).quantize(CENT, rounding=ROUND_HALF_UP)
    down = (base * (1 - ratio)).quantize(CENT, rounding=ROUND_HALF_UP)
    return up, down


@dataclass
class OrderCheck:
    order: dict[str, Any] | None
    reasons: list[str] = field(default_factory=list)


def normalize_order(raw: Any, *, positions: dict[str, dict[str, Any]], open_order_ids: set[str]) -> OrderCheck:
    """Validate one model-proposed order; reject rather than repair."""
    if not isinstance(raw, dict):
        return OrderCheck(None, ["order_not_object"])
    action = str(raw.get("action") or "").lower()
    reason = str(raw.get("reason") or "").strip()
    if action == "cancel":
        order_id = str(raw.get("order_id") or "")
        if order_id not in open_order_ids:
            return OrderCheck(None, ["cancel_unknown_order"])
        return OrderCheck({"action": "cancel", "order_id": order_id, "reason": reason})
    reasons: list[str] = []
    symbol = str(raw.get("symbol") or "").upper()
    if action not in {"buy", "sell"}:
        reasons.append("unsupported_action")
    if not SYMBOL_RE.match(symbol):
        reasons.append("invalid_symbol")
    try:
        quantity = int(raw.get("quantity"))
    except (TypeError, ValueError):
        quantity = 0
    if quantity <= 0:
        reasons.append("non_positive_quantity")
    held = int((positions.get(symbol) or {}).get("quantity") or 0)
    # Odd lots may only be sold as the whole remaining position.
    if quantity % LOT_SIZE and not (action == "sell" and quantity == held):
        reasons.append("not_board_lot")
    order_type = str(raw.get("order_type") or "market").lower()
    if order_type not in {"market", "limit"}:
        reasons.append("unsupported_order_type")
    limit_price = dec(raw.get("limit_price")) if raw.get("limit_price") not in (None, "") else None
    if order_type == "limit" and (limit_price is None or limit_price <= 0):
        reasons.append("limit_price_required")
    if not reason:
        reasons.append("reason_required")
    if reasons:
        return OrderCheck(None, reasons)
    return OrderCheck({
        "action": action, "symbol": symbol, "quantity": quantity, "order_type": order_type,
        "limit_price": limit_price.quantize(CENT) if order_type == "limit" and limit_price is not None else None,
        "reason": reason,
    })


def walk_book(side: str, quantity: int, quote: dict[str, Any], limit_price: Decimal | None) -> tuple[int, Decimal | None]:
    """Fill against visible depth only; sizes are exchange lots of 100 shares."""
    levels = quote.get("asks") if side == "buy" else quote.get("bids")
    remaining, cost = quantity, Decimal("0")
    for level in levels or []:
        price, size_lot = dec(level.get("price")), dec(level.get("size"))
        if price <= 0 or size_lot <= 0:
            continue
        if limit_price is not None and ((side == "buy" and price > limit_price) or (side == "sell" and price < limit_price)):
            break
        take = min(remaining, int(size_lot * LOT_SIZE))
        cost += price * take
        remaining -= take
        if remaining <= 0:
            break
    filled = quantity - remaining
    # A partial board-lot remainder is not tradable; round the fill down.
    if filled != quantity and filled % LOT_SIZE:
        extra = filled % LOT_SIZE
        average = cost / filled
        cost -= average * extra
        filled -= extra
    return filled, (cost / filled) if filled else None


def tradability_reasons(side: str, quote: dict[str, Any] | None, *, sellable: int, quantity: int) -> list[str]:
    reasons: list[str] = []
    if not quote or dec(quote.get("price")) <= 0:
        return ["no_live_quote"]
    if side == "sell" and sellable < quantity:
        reasons.append("t_plus_one_or_insufficient_sellable_quantity")
    up, down = limit_prices(str(quote.get("ts_code") or ""), quote.get("name"), quote.get("pre_close"))
    asks = [row for row in quote.get("asks") or [] if dec(row.get("price")) > 0 and dec(row.get("size")) > 0]
    bids = [row for row in quote.get("bids") or [] if dec(row.get("price")) > 0 and dec(row.get("size")) > 0]
    if side == "buy" and not asks:
        reasons.append("limit_up_or_no_ask_depth" if up is not None and dec(quote.get("price")) >= up else "no_ask_depth")
    if side == "sell" and not bids:
        reasons.append("limit_down_or_no_bid_depth" if down is not None and dec(quote.get("price")) <= down else "no_bid_depth")
    return reasons


def fees_for(side: str, quantity: int, price: Decimal) -> Decimal:
    # The book walk already pays the spread, so no extra slippage is charged.
    return estimate_trade_cost(side=side, quantity=quantity, price=price, slippage_bps=Decimal("0"))["total_cost"].quantize(CENT)


def apply_fill(cash: Decimal, position: dict[str, Any] | None, *, side: str, quantity: int, price: Decimal,
               fees: Decimal) -> tuple[Decimal, dict[str, Any]]:
    """Return new cash and position; buys carry fees in cost, sells realize them."""
    position = dict(position or {"quantity": 0, "sellable_quantity": 0, "average_cost": Decimal("0"), "realized_pnl": Decimal("0")})
    qty, sellable = int(position.get("quantity") or 0), int(position.get("sellable_quantity") or 0)
    avg, realized = dec(position.get("average_cost")), dec(position.get("realized_pnl"))
    gross = price * quantity
    if side == "buy":
        if cash < gross + fees:
            raise ValueError("insufficient_cash")
        new_qty = qty + quantity
        position.update(quantity=new_qty, average_cost=((avg * qty) + gross + fees) / new_qty)
        return cash - gross - fees, position
    if quantity > sellable:
        raise ValueError("insufficient_sellable_quantity")
    position.update(quantity=qty - quantity, sellable_quantity=sellable - quantity,
                    realized_pnl=realized + (price - avg) * quantity - fees)
    return cash + gross - fees, position


def limit_crossed(side: str, limit_price: Decimal, *, low: Decimal | None, high: Decimal | None) -> bool:
    """A resting limit fills only when a later observation traded through it."""
    if side == "buy":
        return low is not None and low > 0 and low <= limit_price
    return high is not None and high > 0 and high >= limit_price


def equity(cash: Decimal, positions: list[dict[str, Any]], prices: dict[str, Decimal]) -> tuple[Decimal, Decimal, list[str]]:
    market_value, missing = Decimal("0"), []
    for row in positions:
        qty = int(row.get("quantity") or 0)
        if qty <= 0:
            continue
        price = prices.get(row["symbol"])
        if price is None or price <= 0:
            missing.append(row["symbol"])
            price = dec(row.get("average_cost"))
        market_value += price * qty
    return cash + market_value, market_value, missing


__all__ = [
    "MAX_FOCUS_SYMBOLS", "MAX_ORDERS_PER_DECISION", "OrderCheck", "SYMBOL_RE", "apply_fill", "dec", "equity",
    "fees_for", "limit_crossed", "limit_prices", "normalize_order", "tradability_reasons", "walk_book",
]
