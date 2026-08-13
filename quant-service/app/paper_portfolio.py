"""Pure paper-portfolio sizing and risk overlay for A-share research.

The overlay is fail-closed and can only reduce a proposal to zero risk.  It
does not represent broker buying power or submit an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from psycopg.types.json import Json


@dataclass(frozen=True)
class PaperRiskDecision:
    allowed: bool
    target_weight: float
    reasons: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def paper_risk_gate(*, signal_type: str, symbol: str, position: dict[str, Any] | None = None,
                    snapshot: dict[str, Any] | None = None, max_target_weight: float = 0.05,
                    max_gross_exposure: float = 0.80, max_drawdown: float = -0.08,
                    max_daily_loss: float = -0.03) -> PaperRiskDecision:
    """Apply deterministic paper limits; no market data is fetched here."""
    position = position or {}
    snapshot = snapshot or {}
    reasons: list[str] = []
    flags: list[str] = []
    if signal_type not in {"entry", "watch", "reduce", "exit"}:
        return PaperRiskDecision(False, 0.0, ("unsupported_signal_type",), ("paper_unsupported_signal",))
    if signal_type in {"reduce", "exit"}:
        return PaperRiskDecision(True, 0.0)
    drawdown = _number(snapshot.get("drawdown"), 0.0)
    daily_return = _number(snapshot.get("daily_return"), 0.0)
    gross = _number(snapshot.get("gross_exposure"), 0.0)
    if drawdown <= max_drawdown:
        reasons.append("portfolio_drawdown_limit")
        flags.append("paper_drawdown_block")
    if daily_return <= max_daily_loss:
        reasons.append("paper_daily_loss_limit")
        flags.append("paper_daily_loss_block")
    if gross >= max_gross_exposure:
        reasons.append("gross_exposure_limit")
        flags.append("paper_gross_exposure_block")
    current_weight = _number(position.get("target_weight"), 0.0)
    if current_weight >= max_target_weight:
        reasons.append("single_symbol_exposure_limit")
        flags.append("paper_symbol_exposure_block")
    if reasons:
        return PaperRiskDecision(False, 0.0, tuple(reasons), tuple(flags))
    # A watch signal is an evidence proposal only; it must never imply a
    # paper position.  Sizing is reserved for an explicit entry contract.
    return PaperRiskDecision(True, max_target_weight if signal_type == "entry" else 0.0)


def mark_to_market(*, positions: Iterable[dict[str, Any]], quotes: dict[str, Any], cash: float,
                   previous_equity: float | None = None, previous_close_equity: float | None = None) -> dict[str, Any]:
    """Calculate a bounded snapshot from local paper positions and quotes."""
    rows: list[dict[str, Any]] = []
    market_value = 0.0
    for position in positions:
        symbol = str(position.get("symbol") or "")
        quantity = max(0, int(position.get("quantity") or 0))
        price = _number((quotes.get(symbol) or {}).get("price"), _number(position.get("average_cost")))
        value = quantity * price
        market_value += value
        rows.append({"symbol": symbol, "quantity": quantity, "price": price, "market_value": round(value, 4)})
    equity = float(cash) + market_value
    peak = max(float(previous_equity or equity), equity)
    drawdown = equity / peak - 1 if peak else 0.0
    daily_return = equity / float(previous_close_equity) - 1 if previous_close_equity else 0.0
    return {"cash": round(float(cash), 4), "equity": round(equity, 4),
            "gross_exposure": round(market_value / equity, 6) if equity else 0.0,
            "net_exposure": round(market_value / equity, 6) if equity else 0.0,
            "drawdown": round(drawdown, 6), "daily_return": round(daily_return, 6),
            "positions": rows}


def persist_portfolio_snapshot(connection: Any, *, as_of: Any, quotes: dict[str, Any],
                              cash: float, previous_equity: float | None = None,
                              previous_close_equity: float | None = None) -> dict[str, Any]:
    # Coalesce high-frequency scans into one durable minute bucket.  This
    # keeps the paper ledger bounded while retaining the minute-level audit
    # needed by the dashboard and barrier labels.
    if hasattr(as_of, "replace"):
        as_of = as_of.replace(second=0, microsecond=0)
    positions = [dict(row) for row in connection.execute(
        "SELECT symbol,quantity,sellable_quantity,average_cost,buy_date,realized_pnl FROM quant.paper_positions"
    ).fetchall()]
    snapshot = mark_to_market(positions=positions, quotes=quotes, cash=cash,
                              previous_equity=previous_equity,
                              previous_close_equity=previous_close_equity)
    connection.execute(
        """INSERT INTO quant.paper_portfolio_snapshots(as_of,cash,equity,gross_exposure,net_exposure,drawdown,payload)
           VALUES(%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(as_of) DO UPDATE SET cash=EXCLUDED.cash,equity=EXCLUDED.equity,
             gross_exposure=EXCLUDED.gross_exposure,net_exposure=EXCLUDED.net_exposure,
             drawdown=EXCLUDED.drawdown,payload=EXCLUDED.payload""",
        (as_of, snapshot["cash"], snapshot["equity"], snapshot["gross_exposure"], snapshot["net_exposure"],
         snapshot["drawdown"], Json({**snapshot, "cash_source": "paper_config_or_prior_snapshot"})),
    )
    return snapshot


__all__ = ["PaperRiskDecision", "mark_to_market", "paper_risk_gate", "persist_portfolio_snapshot"]
