"""Bounded local evidence readiness for one on-demand stock study window."""

from __future__ import annotations

from datetime import date
from typing import Any


_SPECS = (
    ("daily", "日线行情", "P0"),
    ("daily_basic", "估值与换手", "P0"),
    ("stk_limit", "涨跌停价格", "P0"),
    ("moneyflow_dc", "东财主力/散户资金", "P0"),
    ("adj_factor", "复权因子", "P1"),
    ("moneyflow", "Tushare资金流", "P1"),
    ("moneyflow_ths", "同花顺资金流", "P1"),
    ("cyq_perf", "筹码胜率摘要", "P1"),
    ("cyq_chips", "筹码分布明细", "P1"),
    ("stk_factor_pro", "专业技术因子", "P1"),
)


def raw_api_window_summary(connection: Any, api_name: str, symbol: str, start_date: date, end_date: date) -> dict[str, Any]:
    row = connection.execute(
        """SELECT count(*)::int rows,max(row_data->>'trade_date') latest_date
             FROM quant.tushare_raw_records
            WHERE api_name=%s AND row_data->>'ts_code'=%s
              AND row_data->>'trade_date' BETWEEN %s AND %s""",
        (api_name, symbol, start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d")),
    ).fetchone()
    return {"rows": int(row["rows"] or 0), "latest_date": row["latest_date"]}


def stock_window_readiness(database: Any, symbol: str, start_date: date, end_date: date) -> dict[str, Any]:
    """Report only locally persisted evidence; never trigger a provider call."""
    table_by_api = {
        "daily": "quant.canonical_bars_daily",
        "daily_basic": "quant.daily_fundamentals",
        "stk_limit": "quant.daily_trade_limits",
        "adj_factor": "quant.daily_adjustment_factors",
    }
    with database.transaction() as connection:
        items: list[dict[str, Any]] = []
        for api_name, label, priority in _SPECS:
            table = table_by_api.get(api_name)
            if table is not None:
                row = connection.execute(
                    f"""SELECT count(*)::int rows,max(trading_date) latest_date
                         FROM {table}
                        WHERE symbol=%s AND trading_date BETWEEN %s AND %s""",
                    (symbol, start_date, end_date),
                ).fetchone()
                rows, latest_date = int(row["rows"] or 0), row["latest_date"]
            else:
                summary = raw_api_window_summary(connection, api_name, symbol, start_date, end_date)
                rows, latest_date = summary["rows"], summary["latest_date"]
            items.append({"api_name": api_name, "label": label, "priority": priority, "rows": rows,
                          "latest_date": str(latest_date) if latest_date else None,
                          "status": "ready" if rows > 0 else "missing"})
    blockers = [item["api_name"] for item in items if item["priority"] == "P0" and item["status"] != "ready"]
    return {"symbol": symbol, "window_start": str(start_date), "window_end": str(end_date),
            "mode": "on_demand_single_stock_window", "decision_ready": not blockers,
            "blockers": blockers, "items": items}


__all__ = ["raw_api_window_summary", "stock_window_readiness"]
