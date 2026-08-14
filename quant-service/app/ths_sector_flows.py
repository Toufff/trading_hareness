"""Dependency-injected THS sector money-flow materialization."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable


async def sync_industry(
    request: Any,
    *,
    trade_date: Callable[[], Any],
    fetch_catalog: Callable[[Any], Awaitable[dict[str, Any]]],
    fetch_request: Any,
    load_rows: Callable[[str], Awaitable[list[dict[str, Any]]]],
    run_database_blocking: Callable[..., Awaitable[Any]],
    db: Any,
    upsert_taxonomy: Callable[..., Any],
    upsert_sector: Callable[..., Any],
    decimal_or_none: Callable[[Any], Any],
    json_value: Callable[[Any], Any],
    observed_at: Callable[[], datetime],
) -> dict[str, Any]:
    day = request.trade_date or trade_date()
    outcome = await fetch_catalog(fetch_request(api_name="moneyflow_ind_ths", provider=request.provider, params={"trade_date": day.strftime("%Y%m%d")}, max_rows=1000))
    rows = await load_rows(str(outcome["request_key"]))
    valid_rows = [row for row in rows if str(row.get("ts_code") or "").endswith(".TI") and row.get("industry")]
    if not valid_rows:
        return {"status": "blocked", "trade_date": str(day), "reason": "moneyflow_ind_ths returned no valid industry rows", "request_key": outcome["request_key"]}
    provider_key = str(outcome["provider"])
    observed = observed_at()

    def persist_industry_flow() -> None:
        with db.transaction() as connection:
            upsert_taxonomy(connection, "ths_industry", "同花顺行业", provider_key, {"api_name": "moneyflow_ind_ths"})
            for row in valid_rows:
                sector_key, label = str(row["ts_code"]), str(row["industry"])
                upsert_sector(connection, "ths_industry", sector_key, label, {"industry": label})
                connection.execute(
                    """INSERT INTO quant.sector_market_observations(taxonomy_key,sector_key,trading_date,provider_key,available_at,close,change_pct,
                         net_amount,net_buy_amount,net_sell_amount,constituent_count,leading_symbol,leading_label,raw)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,null,%s,%s)
                       ON CONFLICT(taxonomy_key,sector_key,trading_date,provider_key) DO UPDATE SET available_at=EXCLUDED.available_at,
                         close=EXCLUDED.close,change_pct=EXCLUDED.change_pct,net_amount=EXCLUDED.net_amount,net_buy_amount=EXCLUDED.net_buy_amount,
                         net_sell_amount=EXCLUDED.net_sell_amount,constituent_count=EXCLUDED.constituent_count,leading_label=EXCLUDED.leading_label,raw=EXCLUDED.raw""",
                    ("ths_industry", sector_key, day, provider_key, observed, decimal_or_none(row.get("close")),
                     decimal_or_none(row.get("pct_change")), decimal_or_none(row.get("net_amount")), decimal_or_none(row.get("net_buy_amount")),
                     decimal_or_none(row.get("net_sell_amount")), int(row["company_num"]) if row.get("company_num") not in (None, "") else None,
                     row.get("lead_stock"), json_value(row)),
                )
    await run_database_blocking(persist_industry_flow)
    return {"status": outcome["status"], "trade_date": str(day), "taxonomy_key": "ths_industry", "sectors": len(valid_rows),
            "provider": provider_key, "request_key": outcome["request_key"]}


async def sync_concept_signals(
    request: Any,
    *,
    trade_date: Callable[[], Any],
    fetch_catalog: Callable[[Any], Awaitable[dict[str, Any]]],
    fetch_request: Any,
    load_rows: Callable[[str], Awaitable[list[dict[str, Any]]]],
    run_database_blocking: Callable[..., Awaitable[Any]],
    db: Any,
    upsert_taxonomy: Callable[..., Any],
    upsert_sector: Callable[..., Any],
    decimal_or_none: Callable[[Any], Any],
    json_value: Callable[[Any], Any],
    observed_at: Callable[[], datetime],
    http_exception: type[Exception],
) -> dict[str, Any]:
    day = request.trade_date or trade_date()
    stamp = day.strftime("%Y%m%d")
    results: dict[str, dict[str, Any]] = {}
    concept_outcome = await fetch_catalog(fetch_request(api_name="moneyflow_cnt_ths", provider=request.provider, params={"trade_date": stamp}, max_rows=1000))
    concept_rows = [row for row in await load_rows(str(concept_outcome["request_key"])) if str(row.get("ts_code") or "").endswith(".TI") and row.get("name")]
    concept_provider = str(concept_outcome["provider"])
    observed = observed_at()

    def persist_concept_flow() -> None:
        with db.transaction() as connection:
            upsert_taxonomy(connection, "ths_concept_flow", "同花顺概念资金流", concept_provider,
                            {"api_name": "moneyflow_cnt_ths", "semantic": "concept_flow"})
            for row in concept_rows:
                sector_key, label = str(row["ts_code"]), str(row["name"])
                upsert_sector(connection, "ths_concept_flow", sector_key, label, {"name": label})
                connection.execute(
                    """INSERT INTO quant.sector_market_observations(taxonomy_key,sector_key,trading_date,provider_key,available_at,close,change_pct,
                         net_amount,net_buy_amount,net_sell_amount,constituent_count,leading_symbol,leading_label,raw)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,null,%s,%s)
                       ON CONFLICT(taxonomy_key,sector_key,trading_date,provider_key) DO UPDATE SET available_at=EXCLUDED.available_at,
                         close=EXCLUDED.close,change_pct=EXCLUDED.change_pct,net_amount=EXCLUDED.net_amount,net_buy_amount=EXCLUDED.net_buy_amount,
                         net_sell_amount=EXCLUDED.net_sell_amount,constituent_count=EXCLUDED.constituent_count,leading_label=EXCLUDED.leading_label,raw=EXCLUDED.raw""",
                    ("ths_concept_flow", sector_key, day, concept_provider, observed, decimal_or_none(row.get("industry_index")),
                     decimal_or_none(row.get("pct_change")), decimal_or_none(row.get("net_amount")), decimal_or_none(row.get("net_buy_amount")),
                     decimal_or_none(row.get("net_sell_amount")), int(row["company_num"]) if row.get("company_num") not in (None, "") else None,
                     row.get("lead_stock"), json_value(row)),
                )
    await run_database_blocking(persist_concept_flow)
    results["concept_flow"] = {"status": concept_outcome["status"], "taxonomy_key": "ths_concept_flow", "sectors": len(concept_rows),
                                "provider": concept_provider, "request_key": concept_outcome["request_key"]}
    try:
        strength_outcome = await fetch_catalog(fetch_request(api_name="limit_cpt_list", provider=request.provider, params={"trade_date": stamp}, max_rows=1000))
        strength_rows = [row for row in await load_rows(str(strength_outcome["request_key"])) if str(row.get("ts_code") or "").endswith(".TI") and row.get("name")]
        strength_provider = str(strength_outcome["provider"])

        def persist_limit_strength() -> None:
            with db.transaction() as connection:
                upsert_taxonomy(connection, "ths_limit_strength", "同花顺概念涨停强度", strength_provider,
                                {"api_name": "limit_cpt_list", "semantic": "limit_up_strength"})
                for row in strength_rows:
                    sector_key, label = str(row["ts_code"]), str(row["name"])
                    upsert_sector(connection, "ths_limit_strength", sector_key, label, {"name": label})
                    connection.execute(
                        """INSERT INTO quant.sector_market_observations(taxonomy_key,sector_key,trading_date,provider_key,available_at,close,change_pct,
                             net_amount,net_buy_amount,net_sell_amount,constituent_count,leading_symbol,leading_label,raw)
                       VALUES(%s,%s,%s,%s,%s,null,%s,null,null,null,%s,null,null,%s)
                       ON CONFLICT(taxonomy_key,sector_key,trading_date,provider_key) DO UPDATE SET available_at=EXCLUDED.available_at,
                         change_pct=EXCLUDED.change_pct,constituent_count=EXCLUDED.constituent_count,raw=EXCLUDED.raw""",
                        ("ths_limit_strength", sector_key, day, strength_provider, observed, decimal_or_none(row.get("pct_chg")),
                         int(row["cons_nums"]) if row.get("cons_nums") not in (None, "") else None, json_value(row)),
                    )
        await run_database_blocking(persist_limit_strength)
        results["limit_strength"] = {"status": strength_outcome["status"], "taxonomy_key": "ths_limit_strength", "sectors": len(strength_rows),
                                      "provider": strength_provider, "request_key": strength_outcome["request_key"]}
    except http_exception as error:
        results["limit_strength"] = {"status": "failed", "taxonomy_key": "ths_limit_strength", "sectors": 0, "error": str(getattr(error, "detail", error))}
    status = "completed" if all(item["status"] in {"completed", "partial", "unchanged", "empty"} for item in results.values()) else "partial"
    return {"status": status, "trade_date": str(day), "sources": results}


__all__ = ["sync_industry", "sync_concept_signals"]
