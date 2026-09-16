"""Read model for the agent account versus the reconstructed human account."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from .baseline import human_equity
from .rules import dec


def _pct(value: Decimal, base: Decimal) -> float | None:
    return round(float((value / base - 1) * 100), 2) if base > 0 else None


def status(connection: Any, *, account_key: str, day: date | None = None, decisions: int = 20) -> dict[str, Any]:
    account = connection.execute("SELECT * FROM quant.agent_paper_accounts WHERE account_key=%s", (account_key,)).fetchone()
    if account is None:
        return {"status": "not_configured", "account_key": account_key}
    account = dict(account)
    nav_rows = [dict(r) for r in connection.execute(
        """SELECT DISTINCT ON (trading_date) trading_date,as_of,cash,market_value,equity,price_basis
             FROM quant.agent_paper_nav WHERE account_key=%s ORDER BY trading_date,as_of DESC""", (account_key,),
    ).fetchall()]
    day = day or (nav_rows[-1]["trading_date"] if nav_rows else account["start_date"])
    latest = connection.execute(
        "SELECT as_of,equity,cash,market_value,price_basis FROM quant.agent_paper_nav WHERE account_key=%s ORDER BY as_of DESC LIMIT 1",
        (account_key,),
    ).fetchone()
    initial = dec(account["initial_equity"])
    baseline = dict(account["baseline"])
    daily = []
    for row in nav_rows:
        human = human_equity(connection, baseline=baseline, day=row["trading_date"])
        daily.append({
            "trading_date": row["trading_date"].isoformat(), "agent_equity": float(row["equity"]),
            "agent_return_pct": _pct(dec(row["equity"]), initial), "agent_price_basis": row["price_basis"],
            "human_equity": float(human["equity"]), "human_return_pct": _pct(dec(human["equity"]), initial),
            "human_fills_imported_through": human["fills_imported_through"], "human_missing_prices": human["missing_prices"],
        })
    positions = [dict(r) for r in connection.execute(
        """SELECT symbol,name,quantity,sellable_quantity,average_cost,realized_pnl FROM quant.agent_paper_positions
            WHERE account_key=%s AND quantity>0 ORDER BY symbol""", (account_key,)).fetchall()]
    orders = [dict(r) for r in connection.execute(
        """SELECT order_id,placed_at,symbol,name,side,order_type,quantity,limit_price,status,filled_quantity,fill_price,fees,
                  reason,reject_reasons,filled_at FROM quant.agent_paper_orders
            WHERE account_key=%s AND trading_date=%s ORDER BY placed_at""", (account_key, day)).fetchall()]
    recent = [dict(r) for r in connection.execute(
        """SELECT decided_at,status,output->>'market_view' AS market_view,output->>'notes' AS notes,
                  jsonb_array_length(COALESCE(output->'orders','[]'::jsonb)) AS order_count,error,duration_ms,
                  usage->>'total_cost_usd' AS cost_usd
             FROM quant.agent_paper_decisions WHERE account_key=%s ORDER BY decided_at DESC LIMIT %s""",
        (account_key, decisions)).fetchall()]
    counts = connection.execute(
        """SELECT count(*) FILTER (WHERE status='decided') AS decided,count(*) FILTER (WHERE status<>'decided') AS failed
             FROM quant.agent_paper_decisions WHERE account_key=%s AND trading_date=%s""", (account_key, day)).fetchone()
    return {
        "status": "ok", "paper_only": True, "live_effect": "none", "account_key": account_key, "model": account["model"],
        "start_date": account["start_date"].isoformat(), "initial_equity": float(initial), "cash": float(account["cash"]),
        "baseline": baseline, "day": day.isoformat(),
        "latest_nav": ({**dict(latest), "return_pct": _pct(dec(latest["equity"]), initial)} if latest else None),
        "daily": daily, "positions": positions, "orders": orders, "decisions_today": dict(counts), "recent_decisions": recent,
        "comparison_note": "人类收益=券商快照+已导入成交按收盘价重建；某日成交未导入时该日人类收益不准。",
    }


__all__ = ["status"]
