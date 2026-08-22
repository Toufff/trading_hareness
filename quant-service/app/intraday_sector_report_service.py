"""Point-in-time sector report assembly from already fetched snapshots.

The external fetch orchestration remains in the router composition layer.  This
module owns only the synchronous database join and context projection, making
the expensive SQL contract independently testable and keeping provider calls
out of the database worker.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable


def build_intraday_sector_report_from_membership(
    db: Any,
    kinds: tuple[str, ...],
    flow_parts: list[list[dict[str, Any]]],
    quotes: dict[str, dict[str, Any]],
    top_stocks: int,
    exchange_date: date,
    *,
    number: Callable[[Any], float | None],
    ths_top_stocks: Callable[[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]], int], tuple[list[dict[str, Any]], dict[str, int]]],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[Any], list[Any], list[Any]]:
    report: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {}
    with db.transaction() as connection:
        for kind, flows in zip(kinds, flow_parts, strict=True):
            taxonomy_key = f"eastmoney_{kind}"
            rows = connection.execute(
                """SELECT m.sector_key,m.symbol,s.label FROM quant.sector_membership_history m
                   JOIN quant.sectors s ON s.taxonomy_key=m.taxonomy_key AND s.sector_key=m.sector_key
                  WHERE m.taxonomy_key=%s AND m.effective_to IS NULL""",
                (taxonomy_key,),
            ).fetchall()
            by_sector: dict[str, list[str]] = {}
            key_by_label: dict[str, str] = {}
            for row in rows:
                by_sector.setdefault(str(row["sector_key"]), []).append(str(row["symbol"]))
                key_by_label[str(row["label"])] = str(row["sector_key"])
            covered = 0
            for flow in flows:
                label = str(flow.get("行业") or flow.get("板块名称") or "").strip()
                sector_key = str(flow.get("行业代码") or flow.get("板块代码") or key_by_label.get(label) or label).strip()
                stocks = [quotes[symbol] for symbol in by_sector.get(sector_key, []) if symbol in quotes]
                stocks.sort(key=lambda item: (
                    item.get("main_net_inflow") is None,
                    -(item.get("main_net_inflow") or 0),
                    -(item.get("turnover") or 0),
                ))
                covered += int(bool(by_sector.get(sector_key)))
                inflow, outflow = number(flow.get("流入资金")), number(flow.get("流出资金"))
                report.append({
                    "taxonomy_key": taxonomy_key, "sector_key": sector_key, "label": label,
                    "net_inflow": inflow - outflow if inflow is not None and outflow is not None else number(flow.get("净额")),
                    "change_pct": number(flow.get("行业-涨跌幅")),
                    "mapped_members": len(by_sector.get(sector_key, [])), "quoted_members": len(stocks),
                    "top_stocks": stocks[:top_stocks], "member_quotes": stocks,
                })
            coverage[kind] = {"flow_boards": len(flows), "boards_with_members": covered}
        ths_flows = connection.execute(
            """SELECT o.sector_key,s.label,o.net_amount,o.change_pct,o.trading_date
                 FROM quant.sector_market_observations o
                 JOIN quant.sectors s ON s.taxonomy_key=o.taxonomy_key AND s.sector_key=o.sector_key
                WHERE o.taxonomy_key='ths_concept_flow' AND o.trading_date=%s
                ORDER BY o.net_amount DESC NULLS LAST,o.sector_key""",
            (exchange_date,),
        ).fetchall()
        ths_members = connection.execute(
            """SELECT sector_key,symbol FROM quant.sector_membership_history
                 WHERE taxonomy_key='ths_concept_flow' AND effective_to IS NULL""",
        ).fetchall()
        ths_items, coverage["ths_concept"] = ths_top_stocks(
            [dict(row) for row in ths_flows], [dict(row) for row in ths_members], quotes, top_stocks,
        )
        report.extend(ths_items)
        tushare_sector_context = connection.execute(
            """SELECT taxonomy_key,max(trading_date) AS latest_trade_date,count(*)::int AS rows
                 FROM quant.sector_market_observations
                WHERE taxonomy_key IN ('ths_industry','ths_concept_flow')
                GROUP BY taxonomy_key ORDER BY taxonomy_key""",
        ).fetchall()
        tushare_stock_context = connection.execute(
            """SELECT api_name,max(NULLIF(row_data->>'trade_date','')) AS latest_trade_date,
                      count(DISTINCT row_data->>'ts_code')::int AS symbols,count(*)::int AS rows
                 FROM quant.tushare_raw_records
                WHERE api_name IN ('moneyflow','moneyflow_ths','moneyflow_dc')
                GROUP BY api_name ORDER BY api_name""",
        ).fetchall()
        tushare_realtime_context = connection.execute(
            """SELECT api_name,max(available_at) AS latest_available_at,count(*)::int AS rows
                 FROM quant.tushare_raw_records WHERE api_name IN ('rt_k','rt_min','rt_min_daily')
                GROUP BY api_name ORDER BY api_name""",
        ).fetchall()
    return report, coverage, tushare_sector_context, tushare_stock_context, tushare_realtime_context


__all__ = ["build_intraday_sector_report_from_membership"]
