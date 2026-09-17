"""Human-account baseline and reconstructed human equity for comparison.

Broker snapshots are manual, so both the agent's starting book and the human
equity curve are reconstructed: the latest verified snapshot, plus imported
fills after it, marked at daily closes.  Every result states that basis and
the last date fills were imported through; an unimported day is not compared.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .rules import CENT, dec, fees_for

SHANGHAI = ZoneInfo("Asia/Shanghai")


def replay_fills(cash: Decimal, positions: dict[str, dict[str, Any]], fills: list[dict[str, Any]]) -> tuple[Decimal, dict[str, dict[str, Any]]]:
    """Apply broker fills in time order; missing broker fees are estimated."""
    book = {symbol: dict(row) for symbol, row in positions.items()}
    for fill in fills:
        side, qty, price = str(fill["side"]), int(dec(fill["quantity"])), dec(fill["price"])
        recorded = sum((dec(fill.get(key)) for key in ("commission", "stamp_duty", "transfer_fee", "other_fee")), Decimal("0"))
        fees = recorded if recorded > 0 else fees_for(side, qty, price)
        row = book.setdefault(fill["symbol"], {"symbol": fill["symbol"], "name": fill.get("name"), "quantity": 0,
                                               "sellable_quantity": 0, "average_cost": Decimal("0")})
        held, avg = int(row["quantity"]), dec(row["average_cost"])
        sellable = int(row.get("sellable_quantity", held) or 0)
        if side == "buy":
            # Same-day buys stay unsellable (T+1).
            row["quantity"] = held + qty
            row["sellable_quantity"] = sellable
            row["average_cost"] = (avg * held + price * qty + fees) / (held + qty)
            cash -= price * qty + fees
        else:
            row["quantity"] = held - qty
            row["sellable_quantity"] = max(0, sellable - qty)
            cash += price * qty - fees
    return cash, {symbol: row for symbol, row in book.items() if int(row["quantity"]) > 0}


def latest_verified_snapshot(connection: Any, *, account_key: str, before: datetime) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT snapshot_id,observed_at,cash,total_asset,total_market_value FROM quant.broker_portfolio_snapshots
            WHERE account_key=%s AND verification='verified_exact' AND observed_at<=%s
            ORDER BY observed_at DESC LIMIT 1""", (account_key, before),
    ).fetchone()
    if row is None:
        return None
    snapshot = dict(row)
    snapshot["positions"] = {
        position["symbol"]: dict(position) for position in connection.execute(
            """SELECT symbol,name,quantity::int AS quantity,sellable_quantity::int AS sellable_quantity,average_cost,market_price
                 FROM quant.broker_position_snapshots
                WHERE snapshot_id=%s AND quantity>0""", (snapshot["snapshot_id"],),
        ).fetchall()
    }
    return snapshot


def human_fills(connection: Any, *, account_key: str, after: datetime, until: datetime) -> list[dict[str, Any]]:
    """Exact daily executions first; order-history executions only for dates without them."""
    exact = [dict(row) for row in connection.execute(
        """SELECT trade_date,trade_time,symbol,name,side,quantity,price,commission,stamp_duty,transfer_fee,other_fee
             FROM quant.broker_trade_records
            WHERE account_key=%s AND (trade_date + trade_time) AT TIME ZONE 'Asia/Shanghai' > %s
              AND (trade_date + trade_time) AT TIME ZONE 'Asia/Shanghai' <= %s
            ORDER BY trade_date,trade_time""", (account_key, after, until),
    ).fetchall()]
    exact_dates = {row["trade_date"] for row in exact}
    history = [dict(row) for row in connection.execute(
        """SELECT (order_at AT TIME ZONE 'Asia/Shanghai')::date AS trade_date,(order_at AT TIME ZONE 'Asia/Shanghai')::time AS trade_time,
                  symbol,side,filled_quantity AS quantity,fill_price AS price
             FROM quant.broker_order_events
            WHERE account_key=%s AND is_execution AND order_at>%s AND order_at<=%s
            ORDER BY order_at""", (account_key, after, until),
    ).fetchall()]
    fills = exact + [row for row in history if row["trade_date"] not in exact_dates]
    return sorted(fills, key=lambda row: (row["trade_date"], row["trade_time"]))


def fills_imported_through(connection: Any, *, account_key: str) -> date | None:
    row = connection.execute(
        """SELECT GREATEST((SELECT max(trade_date) FROM quant.broker_trade_records WHERE account_key=%s),
                           (SELECT max((order_at AT TIME ZONE 'Asia/Shanghai')::date) FROM quant.broker_order_events
                             WHERE account_key=%s AND is_execution)) AS through""", (account_key, account_key),
    ).fetchone()
    return row["through"] if row else None


def build_baseline(connection: Any, *, source_account: str, start_at: datetime) -> dict[str, Any]:
    """The human book at ``start_at``: latest verified snapshot plus fills between them."""
    snapshot = latest_verified_snapshot(connection, account_key=source_account, before=start_at)
    if snapshot is None:
        raise ValueError("no verified broker snapshot before the start time")
    same_day = snapshot["observed_at"].astimezone(SHANGHAI).date() == start_at.astimezone(SHANGHAI).date()
    positions = {}
    for symbol, row in snapshot["positions"].items():
        # A prior-day book is fully sellable today; a same-day snapshot keeps its T+1 split.
        sellable = int(row["sellable_quantity"] or 0) if same_day else int(row["quantity"])
        cost = row["average_cost"] if row["average_cost"] is not None else row["market_price"]
        positions[symbol] = {**row, "sellable_quantity": sellable, "average_cost": dec(cost),
                             "cost_basis": "broker" if row["average_cost"] is not None else "missing_broker_cost_used_snapshot_price"}
    # Displayed cash can exclude frozen/in-transit balances; equity minus
    # market value is the cash that reconciles with total assets.
    cash = dec(snapshot["total_asset"]) - dec(snapshot["total_market_value"])
    fills = human_fills(connection, account_key=source_account, after=snapshot["observed_at"], until=start_at)
    cash, positions = replay_fills(cash, positions, fills)
    return {
        "source_account": source_account,
        "start_at": start_at.isoformat(),
        "snapshot_id": str(snapshot["snapshot_id"]),
        "snapshot_observed_at": snapshot["observed_at"].isoformat(),
        "snapshot_displayed_cash": str(snapshot["cash"]),
        "snapshot_total_asset": str(snapshot["total_asset"]),
        "replayed_fills": len(fills),
        "cash": cash.quantize(CENT),
        "positions": [{"symbol": symbol, "name": row.get("name"), "quantity": int(row["quantity"]),
                       "sellable_quantity": int(row.get("sellable_quantity", row["quantity"])),
                       "average_cost": dec(row["average_cost"]).quantize(Decimal("0.0001")),
                       "snapshot_price": (str(row["market_price"]) if same_day and not fills and row.get("market_price") is not None
                                          else None),
                       "cost_basis": row.get("cost_basis", "replayed_fill")}
                      for symbol, row in sorted(positions.items())],
        "basis": "verified broker snapshot + imported fills until start_at; cash = total_asset - market_value",
    }


def daily_closes(connection: Any, symbols: list[str], day: date) -> dict[str, Decimal]:
    if not symbols:
        return {}
    rows = connection.execute(
        """SELECT DISTINCT ON (symbol) symbol,close FROM quant.canonical_bars_daily
            WHERE symbol = ANY(%s) AND trading_date<=%s ORDER BY symbol,trading_date DESC""", (symbols, day),
    ).fetchall()
    return {row["symbol"]: dec(row["close"]) for row in rows}


def human_equity(connection: Any, *, baseline: dict[str, Any], day: date,
                 live_prices: dict[str, Decimal] | None = None) -> dict[str, Any]:
    """Reconstructed human equity at a day's close (or live, when prices are given)."""
    source = baseline["source_account"]
    through = fills_imported_through(connection, account_key=source)
    positions = {row["symbol"]: dict(row) for row in baseline["positions"]}
    until = datetime.combine(day, time(23, 59), SHANGHAI)
    # Replay only fills after the start time so the start book is not double counted.
    cutoff = datetime.fromisoformat(baseline["start_at"])
    fills = human_fills(connection, account_key=source, after=cutoff, until=until)
    cash, book = replay_fills(dec(baseline["cash"]), positions, fills)
    prices = dict(live_prices or {})
    closes = daily_closes(connection, [s for s in book if s not in prices], day)
    prices.update(closes)
    market_value, missing = Decimal("0"), []
    for symbol, row in book.items():
        price = prices.get(symbol)
        if price is None:
            missing.append(symbol)
            continue
        market_value += price * int(row["quantity"])
    return {
        "day": day.isoformat(), "cash": cash.quantize(CENT), "market_value": market_value.quantize(CENT),
        "equity": (cash + market_value).quantize(CENT), "fills": len(fills), "missing_prices": missing,
        "fills_imported_through": through.isoformat() if through else None,
        "comparable": bool(through and through >= day) and not missing,
    }


__all__ = ["build_baseline", "daily_closes", "fills_imported_through", "human_equity", "human_fills",
           "latest_verified_snapshot", "replay_fills"]
