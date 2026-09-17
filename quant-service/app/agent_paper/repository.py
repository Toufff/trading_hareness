"""Agent paper ledger persistence (synchronous psycopg, dict rows)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from psycopg.types.json import Json

LOCK_NAMESPACE = 780917


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def create_account(connection: Any, *, account_key: str, model: str, start_date: date, baseline: dict[str, Any],
                   initial_equity: Decimal) -> None:
    exists = connection.execute("SELECT 1 FROM quant.agent_paper_accounts WHERE account_key=%s", (account_key,)).fetchone()
    if exists:
        raise ValueError("agent paper account already exists; it is never reset in place")
    connection.execute(
        """INSERT INTO quant.agent_paper_accounts(account_key,model,start_date,initial_equity,cash,baseline,last_roll_date)
           VALUES(%s,%s,%s,%s,%s,%s,%s)""",
        (account_key, model, start_date, initial_equity, baseline["cash"], Json(_jsonable({**baseline, "start_date": start_date})),
         start_date),
    )
    for row in baseline["positions"]:
        sellable = int(row.get("sellable_quantity", row["quantity"]))
        # Unsellable shares were bought on the start date and roll on the next trading day.
        connection.execute(
            """INSERT INTO quant.agent_paper_positions(account_key,symbol,name,quantity,sellable_quantity,average_cost,last_buy_date)
               VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (account_key, row["symbol"], row.get("name"), row["quantity"], sellable, row["average_cost"],
             start_date if sellable < row["quantity"] else None),
        )


def try_lock(connection: Any, account_key: str) -> bool:
    row = connection.execute("SELECT pg_try_advisory_xact_lock(%s, hashtext(%s)) AS locked", (LOCK_NAMESPACE, account_key)).fetchone()
    return bool(row["locked"])


def load_account(connection: Any, account_key: str, *, for_update: bool = False) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM quant.agent_paper_accounts WHERE account_key=%s" + (" FOR UPDATE" if for_update else ""), (account_key,),
    ).fetchone()
    return dict(row) if row else None


def load_positions(connection: Any, account_key: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(
        "SELECT * FROM quant.agent_paper_positions WHERE account_key=%s ORDER BY symbol", (account_key,),
    ).fetchall()]


def roll_t_plus_one(connection: Any, account_key: str, trading_date: date) -> bool:
    account = load_account(connection, account_key, for_update=True)
    if account is None or (account["last_roll_date"] and account["last_roll_date"] >= trading_date):
        return False
    connection.execute(
        """UPDATE quant.agent_paper_positions SET sellable_quantity=quantity,updated_at=now()
            WHERE account_key=%s AND (last_buy_date IS NULL OR last_buy_date<%s)""", (account_key, trading_date),
    )
    connection.execute("DELETE FROM quant.agent_paper_positions WHERE account_key=%s AND quantity=0", (account_key,))
    connection.execute("UPDATE quant.agent_paper_accounts SET last_roll_date=%s,updated_at=now() WHERE account_key=%s",
                       (trading_date, account_key))
    return True


def expire_orders(connection: Any, account_key: str, trading_date: date, at: datetime) -> int:
    result = connection.execute(
        """UPDATE quant.agent_paper_orders SET status='expired',closed_at=%s,updated_at=now()
            WHERE account_key=%s AND status='open' AND trading_date<=%s""", (at, account_key, trading_date),
    )
    return int(result.rowcount or 0)


def open_orders(connection: Any, account_key: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(
        "SELECT * FROM quant.agent_paper_orders WHERE account_key=%s AND status='open' ORDER BY placed_at", (account_key,),
    ).fetchall()]


def day_orders(connection: Any, account_key: str, trading_date: date) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(
        "SELECT * FROM quant.agent_paper_orders WHERE account_key=%s AND trading_date=%s ORDER BY placed_at",
        (account_key, trading_date),
    ).fetchall()]


def recent_decisions(connection: Any, account_key: str, limit: int = 6) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT decided_at,status,output->>'market_view' AS market_view,output->'orders' AS orders,error
             FROM quant.agent_paper_decisions WHERE account_key=%s ORDER BY decided_at DESC LIMIT %s""", (account_key, limit),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def last_decision_at(connection: Any, account_key: str) -> datetime | None:
    row = connection.execute(
        "SELECT max(decided_at) AS at FROM quant.agent_paper_decisions WHERE account_key=%s", (account_key,),
    ).fetchone()
    return row["at"] if row else None


def insert_decision(connection: Any, *, account_key: str, decided_at: datetime, trading_date: date, status: str, model: str,
                    context_hash: str | None, context_chars: int | None, output: dict[str, Any] | None, error: str | None,
                    usage: dict[str, Any] | None, duration_ms: int | None) -> str:
    row = connection.execute(
        """INSERT INTO quant.agent_paper_decisions(account_key,decided_at,trading_date,status,model,context_hash,context_chars,
                 output,error,usage,duration_ms)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING decision_id""",
        (account_key, decided_at, trading_date, status, model, context_hash, context_chars,
         Json(_jsonable(output)) if output is not None else None, error, Json(_jsonable(usage or {})), duration_ms),
    ).fetchone()
    return str(row["decision_id"])


def update_memory(connection: Any, account_key: str, memory: dict[str, Any]) -> None:
    connection.execute("UPDATE quant.agent_paper_accounts SET memory=%s,updated_at=now() WHERE account_key=%s",
                       (Json(_jsonable(memory)), account_key))


def insert_order(connection: Any, *, account_key: str, decision_id: str | None, trading_date: date, order: dict[str, Any],
                 status: str, placed_at: datetime, reject_reasons: list[str], quote: dict[str, Any], name: str | None) -> str:
    row = connection.execute(
        """INSERT INTO quant.agent_paper_orders(account_key,decision_id,trading_date,symbol,name,side,order_type,quantity,
                 limit_price,status,reason,reject_reasons,quote,placed_at,closed_at)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING order_id""",
        (account_key, decision_id, trading_date, order["symbol"], name, order["action"], order["order_type"], order["quantity"],
         order.get("limit_price"), status, order.get("reason"), Json(reject_reasons), Json(_jsonable(quote)), placed_at,
         placed_at if status in {"rejected"} else None),
    ).fetchone()
    return str(row["order_id"])


def record_fill(connection: Any, *, account_key: str, order_id: str, symbol: str, name: str | None, side: str, quantity: int,
                price: Decimal, fees: Decimal, cash: Decimal, position: dict[str, Any], filled_at: datetime,
                trading_date: date, requested: int) -> None:
    status = "filled" if quantity == requested else "partially_filled"
    connection.execute(
        """UPDATE quant.agent_paper_orders SET status=%s,filled_quantity=%s,fill_price=%s,fees=%s,filled_at=%s,closed_at=%s,
                  updated_at=now() WHERE order_id=%s""",
        (status, quantity, price, fees, filled_at, filled_at, order_id),
    )
    connection.execute("UPDATE quant.agent_paper_accounts SET cash=%s,updated_at=now() WHERE account_key=%s", (cash, account_key))
    connection.execute(
        """INSERT INTO quant.agent_paper_positions(account_key,symbol,name,quantity,sellable_quantity,average_cost,realized_pnl,last_buy_date)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(account_key,symbol) DO UPDATE SET quantity=EXCLUDED.quantity,sellable_quantity=EXCLUDED.sellable_quantity,
             average_cost=EXCLUDED.average_cost,realized_pnl=EXCLUDED.realized_pnl,
             last_buy_date=COALESCE(EXCLUDED.last_buy_date,quant.agent_paper_positions.last_buy_date),
             name=COALESCE(EXCLUDED.name,quant.agent_paper_positions.name),updated_at=now()""",
        (account_key, symbol, name, int(position["quantity"]), int(position.get("sellable_quantity") or 0),
         position["average_cost"], position.get("realized_pnl") or 0, trading_date if side == "buy" else None),
    )


def close_order(connection: Any, order_id: str, status: str, at: datetime, reasons: list[str] | None = None) -> None:
    connection.execute(
        """UPDATE quant.agent_paper_orders SET status=%s,closed_at=%s,
                  reject_reasons=CASE WHEN %s::jsonb IS NULL THEN reject_reasons ELSE %s::jsonb END,updated_at=now()
            WHERE order_id=%s AND status='open'""",
        (status, at, Json(reasons) if reasons is not None else None, Json(reasons) if reasons is not None else None, order_id),
    )


def insert_nav(connection: Any, *, account_key: str, as_of: datetime, trading_date: date, cash: Decimal,
               market_value: Decimal, equity: Decimal, price_basis: str, positions: list[dict[str, Any]]) -> None:
    connection.execute(
        """INSERT INTO quant.agent_paper_nav(account_key,as_of,trading_date,cash,market_value,equity,price_basis,positions)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(account_key,as_of) DO NOTHING""",
        (account_key, as_of, trading_date, cash, market_value, equity, price_basis, Json(_jsonable(positions))),
    )


def quote_range_since(connection: Any, symbol: str, since: datetime, until: datetime) -> tuple[Decimal | None, Decimal | None]:
    """Observed traded-price range between matching passes, from any local collector."""
    row = connection.execute(
        """SELECT min(price) AS low,max(price) AS high FROM quant.intraday_quote_observations
            WHERE symbol=%s AND observed_at>%s AND observed_at<=%s AND price>0""", (symbol, since, until),
    ).fetchone()
    return (row["low"], row["high"]) if row else (None, None)


__all__ = [
    "close_order", "create_account", "day_orders", "expire_orders", "insert_decision", "insert_nav", "insert_order",
    "last_decision_at", "load_account", "load_positions", "open_orders", "quote_range_since", "recent_decisions",
    "record_fill", "roll_t_plus_one", "try_lock", "update_memory",
]
