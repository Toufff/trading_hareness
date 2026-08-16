"""Manual-only paper order acceptance and deterministic local fill simulation."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg.types.json import Json

from .paper_execution import estimate_cost, paper_tradability, round_lot


def _number(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:  # noqa: BLE001 - inputs are audited as non-fill below
        return Decimal("0")


def configure_paper_account(connection: Any, *, account_key: str, initial_cash: Decimal,
                            configured_by: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    if initial_cash < 0:
        raise ValueError("initial_cash must be non-negative")
    existing = connection.execute(
        "SELECT cash FROM quant.paper_accounts WHERE account_key=%s FOR UPDATE", (account_key,)
    ).fetchone()
    if existing is not None:
        filled = connection.execute("SELECT EXISTS(SELECT 1 FROM quant.paper_order_fills)").fetchone()
        if bool(filled and (filled.get("exists") if hasattr(filled, "get") else filled[0])):
            raise ValueError("paper account has filled activity; cash reset is blocked")
    row = connection.execute(
        """INSERT INTO quant.paper_accounts(account_key,initial_cash,cash,configured_by,metadata)
           VALUES(%s,%s,%s,%s,%s)
           ON CONFLICT(account_key) DO UPDATE SET initial_cash=EXCLUDED.initial_cash,cash=EXCLUDED.cash,
             configured_by=EXCLUDED.configured_by,configured_at=now(),updated_at=now(),metadata=EXCLUDED.metadata
           RETURNING account_key,initial_cash,cash,configured_by,configured_at,updated_at,metadata""",
        (account_key, initial_cash, initial_cash, configured_by, Json(metadata or {})),
    ).fetchone()
    return dict(row)


def roll_paper_positions_sellable(connection: Any, *, trading_date: Any) -> int:
    """Apply A-share T+1 at the local trading-date boundary, never intraday."""
    result = connection.execute(
        """UPDATE quant.paper_positions
              SET sellable_quantity=quantity,updated_at=now()
            WHERE quantity>sellable_quantity AND buy_date IS NOT NULL AND buy_date<%s""",
        (trading_date,),
    )
    return max(0, int(result.rowcount or 0))


def _latest_local_quote(connection: Any, symbol: str, at_or_before: datetime) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT source_name,observed_at,price,pct_change,raw FROM quant.intraday_quote_observations
             WHERE symbol=%s AND observed_at<=%s AND source_name IN ('tencent_free','tushare_super_get_rt_k')
             ORDER BY observed_at DESC LIMIT 1""", (symbol, at_or_before),
    ).fetchone()
    if row is None:
        return None
    quote = dict(row)
    raw = quote.get("raw")
    if isinstance(raw, dict):
        # The captured provider flags are required for the hard suspension and
        # limit-price gates; explicit quote columns remain authoritative.
        quote = {**raw, **quote}
    return quote


def accept_paper_decision(connection: Any, *, decision_id: Any, quantity: int,
                          accepted_at: datetime, account_key: str = "default") -> dict[str, Any]:
    """Accept once; simulate only against evidence that already existed by acceptance time."""
    decision = connection.execute(
        """SELECT decision_id,symbol,direction,status,decision_at,evidence,risk_flags
             FROM quant.paper_decisions WHERE decision_id=%s FOR UPDATE""", (decision_id,)
    ).fetchone()
    if decision is None:
        raise ValueError("paper decision was not found")
    decision = dict(decision)
    if decision["status"] != "proposed":
        raise ValueError("only proposed paper decisions may be accepted")
    side = "buy" if int(decision["direction"] or 0) > 0 else "sell" if int(decision["direction"] or 0) < 0 else None
    if side is None:
        raise ValueError("watch decisions have no executable paper side")
    requested = round_lot(int(quantity))
    if requested <= 0:
        raise ValueError("quantity must contain at least one A-share board lot")
    quote = _latest_local_quote(connection, str(decision["symbol"]), accepted_at)
    position = connection.execute(
        "SELECT symbol,quantity,sellable_quantity,average_cost,buy_date,realized_pnl FROM quant.paper_positions WHERE symbol=%s FOR UPDATE",
        (decision["symbol"],),
    ).fetchone()
    position_dict = dict(position) if position else {"sellable_quantity": 0, "quantity": 0, "average_cost": 0}
    tradability = paper_tradability(
        side=side, requested_quantity=requested, quote=quote or {}, position=position_dict,
        symbol=str(decision["symbol"]),
    )
    account = connection.execute("SELECT account_key,cash FROM quant.paper_accounts WHERE account_key=%s FOR UPDATE", (account_key,)).fetchone()
    reasons = list(tradability.reasons)
    quote_price = _number((quote or {}).get("price"))
    costs = estimate_cost(side=side, quantity=requested, price=quote_price) if quote_price > 0 else {"total_cost": Decimal("0"), "slippage": Decimal("0")}
    gross = quote_price * requested
    if account is None:
        reasons.append("paper_account_not_configured")
    if quote is None or quote_price <= 0:
        reasons.append("no_usable_local_quote")
    elif side == "buy" and _number(account["cash"]) < gross + _number(costs["total_cost"]):
        reasons.append("insufficient_paper_cash")
    allowed = bool(tradability.allowed and not reasons)
    fees = _number(costs["total_cost"])
    order = connection.execute(
        """INSERT INTO quant.paper_orders(decision_id,symbol,side,requested_quantity,accepted_quantity,filled_quantity,
                 limit_price,average_fill_price,status,fees,slippage,submitted_at,filled_at,metadata)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING order_id""",
        (decision["decision_id"], decision["symbol"], side, requested, requested if allowed else 0, requested if allowed else 0,
         quote_price if quote_price > 0 else None, quote_price if allowed else None,
         "filled" if allowed else "non_fill", fees if allowed else Decimal("0"), _number(costs.get("slippage")), accepted_at,
         accepted_at if allowed else None, Json({"manual_acceptance": True, "reason_codes": reasons,
                                                "quote_source": (quote or {}).get("source_name")})),
    ).fetchone()
    if not allowed:
        connection.execute("UPDATE quant.paper_decisions SET status='blocked' WHERE decision_id=%s", (decision["decision_id"],))
        return {"status": "non_fill", "order_id": str(order["order_id"]), "reason_codes": reasons, "tradability": tradability.__dict__}
    connection.execute(
        """INSERT INTO quant.paper_order_fills(order_id,decision_id,symbol,side,quantity,price,fees,slippage,filled_at,
                   source_name,quote_observed_at,metadata)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (order["order_id"], decision["decision_id"], decision["symbol"], side, requested, quote_price, fees,
         _number(costs.get("slippage")), accepted_at, quote["source_name"], quote["observed_at"],
         Json({"paper_only": True, "quote_age_seconds": max(0, int((accepted_at - quote["observed_at"]).total_seconds()))})),
    )
    if side == "buy":
        old_qty, old_cost = int(position_dict["quantity"] or 0), _number(position_dict["average_cost"])
        total_qty = old_qty + requested
        average_cost = ((old_qty * old_cost) + gross + fees) / total_qty
        connection.execute(
            """INSERT INTO quant.paper_positions(symbol,quantity,sellable_quantity,average_cost,buy_date,realized_pnl,updated_at)
               VALUES(%s,%s,0,%s,(%s AT TIME ZONE 'Asia/Shanghai')::date,0,now())
               ON CONFLICT(symbol) DO UPDATE SET quantity=EXCLUDED.quantity,average_cost=EXCLUDED.average_cost,
                 buy_date=EXCLUDED.buy_date,updated_at=now()""",
            (decision["symbol"], total_qty, average_cost, accepted_at),
        )
        connection.execute("UPDATE quant.paper_accounts SET cash=cash-%s,updated_at=now() WHERE account_key=%s", (gross + fees, account_key))
    else:
        old_qty, sellable, old_cost = int(position_dict["quantity"] or 0), int(position_dict["sellable_quantity"] or 0), _number(position_dict["average_cost"])
        if requested > sellable:
            raise RuntimeError("tradability gate allowed an unsellable quantity")
        realized = (quote_price - old_cost) * requested - fees
        connection.execute(
            """UPDATE quant.paper_positions SET quantity=%s,sellable_quantity=%s,realized_pnl=realized_pnl+%s,updated_at=now()
                 WHERE symbol=%s""", (old_qty - requested, sellable - requested, realized, decision["symbol"]),
        )
        connection.execute("UPDATE quant.paper_accounts SET cash=cash+%s,updated_at=now() WHERE account_key=%s", (gross - fees, account_key))
    connection.execute("UPDATE quant.paper_decisions SET status='accepted' WHERE decision_id=%s", (decision["decision_id"],))
    return {"status": "filled", "order_id": str(order["order_id"]), "quantity": requested, "price": float(quote_price),
            "fees": float(fees), "source": quote["source_name"], "paper_only": True}


__all__ = ["accept_paper_decision", "configure_paper_account", "roll_paper_positions_sellable"]
