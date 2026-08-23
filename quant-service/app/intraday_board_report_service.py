"""Durable intraday board-report orchestration.

The report is frontend evidence only: board and limit-linkage mining must not
become an unsolicited Feishu stream.  External collection, provider limits and
market-session gating are injected by the application composition layer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
import uuid
from zoneinfo import ZoneInfo

from psycopg.types.json import Json


AsyncCall = Callable[..., Awaitable[Any]]


def summary_sections(
    items: list[dict[str, Any]], flow_label: Callable[[Any], str],
) -> tuple[dict[str, Any], list[str]]:
    """Build the compact industry/concept flow summary from bounded items."""
    sections: list[str] = []
    summary: dict[str, Any] = {}
    for taxonomy_key, label in (("eastmoney_industry", "行业"), ("eastmoney_concept", "概念")):
        rows = [item for item in items if item.get("taxonomy_key") == taxonomy_key and item.get("net_inflow") is not None]
        inflow = sorted(rows, key=lambda item: float(item["net_inflow"]), reverse=True)[:3]
        outflow = sorted(rows, key=lambda item: float(item["net_inflow"]))[:3]
        summary[taxonomy_key] = {"inflow": inflow, "outflow": outflow}

        def render(group: list[dict[str, Any]]) -> str:
            return "；".join(f"{item['label']} {flow_label(item['net_inflow'])}" for item in group) or "—"

        sections.extend([f"{label}流入：{render(inflow)}", f"{label}流出：{render(outflow)}"])
    return summary, sections


async def run(
    *,
    database: Any,
    fetch_report: AsyncCall,
    board_candidates: Callable[[list[dict[str, Any]]], tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]],
    persist_mining_run: Callable[..., str],
    refresh_limit_anchors: AsyncCall,
    run_limit_linkage: AsyncCall,
    run_database: AsyncCall,
    json_safe: Callable[[Any], Any],
    flow_label: Callable[[Any], str],
    number: Callable[[Any], float | None],
    safe_error: Callable[[str, int], str],
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Collect and persist one bounded board/reporting cycle."""
    observed_at = observed_at or datetime.now(timezone.utc)
    report = await fetch_report()
    report_id = uuid.uuid4()
    if report.get("status") != "completed":
        def persist_blocked() -> None:
            with database.transaction() as connection:
                connection.execute(
                    """INSERT INTO quant.intraday_board_reports(board_report_id,observed_at,status,source_status,summary,payload)
                       VALUES(%s,%s,'blocked',%s,%s,%s)""",
                    (report_id, observed_at, Json(json_safe(report.get("sources", {}))),
                     Json({"reason": report.get("reason")}), Json(json_safe(report))),
                )
        await run_database(persist_blocked)
        return {"status": "blocked", "board_report_id": str(report_id), "reason": report.get("reason")}

    runtime_quotes = report.pop("_runtime_quotes", {})
    mining_candidates, mining_coverage, mining_summary = board_candidates(report["items"])
    stored_report = {
        **report,
        "items": [{key: value for key, value in item.items() if key != "member_quotes"} for item in report["items"]],
    }
    summary, sections = summary_sections(report["items"], flow_label)

    def persist_completed() -> None:
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO quant.intraday_board_reports(board_report_id,observed_at,status,source_status,summary,payload)
                   VALUES(%s,%s,'completed',%s,%s,%s)""",
                (report_id, observed_at,
                 Json(json_safe({"coverage": report.get("coverage"), "tushare_context": report.get("tushare_context")})),
                 Json(json_safe(summary)), Json(json_safe(stored_report))),
            )
    await run_database(persist_completed)

    mining = {"status": "completed", "summary": mining_summary, "coverage": mining_coverage}
    try:
        def persist_mining() -> str:
            with database.transaction() as connection:
                return persist_mining_run(
                    connection, board_report_id=report_id, observed_at=observed_at,
                    candidates=mining_candidates, coverage=mining_coverage, summary=mining_summary,
                )
        mining["mining_run_id"] = await run_database(persist_mining)
    except Exception as error:  # The durable board report must survive mining persistence faults.
        mining = {"status": "partial", "reason": safe_error(str(error), 300),
                  "summary": mining_summary, "coverage": mining_coverage}

    limit_anchor_refresh = await refresh_limit_anchors(observed_at)
    linkage = await run_limit_linkage(observed_at, runtime_quotes)
    linkage_candidates = linkage.get("candidates") or []
    if linkage.get("status") == "completed" and linkage_candidates:
        rendered = "；".join(
            f"{item.get('name') or item['symbol']} {item['symbol']}（{number(item.get('pct_change')) or 0.0:+.2f}% / "
            f"量比{number(item.get('volume_ratio')) or 0.0:.2f} / 分{number(item.get('score')) or 0.0:.0f}）"
            for item in linkage_candidates[:20]
        )
        sections.append(f"涨停关联候选（Top{min(20, len(linkage_candidates))}）：{rendered}")
    elif linkage.get("status") == "completed":
        sections.append("涨停关联候选：本轮无满足严格量价门槛的非涨停标的")
    local_time = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
    text = "\n".join([
        f"【盘中板块与关联挖掘快报｜{local_time}】", *sections,
        "来源：东财实时板块资金流、东财涨停池、同花顺精确概念成员、腾讯全 A 行情；关联候选仅供研究，须经分钟承接确认，不构成买卖指令。",
    ])
    # Keep the rendered text for compatible callers/debuggers while explicitly
    # returning a suppressed delivery contract under the watched-stock policy.
    _ = text
    delivery = {"status": "suppressed", "reason": "Feishu is reserved for watched-stock strategy signals"}
    return {"status": "completed", "board_report_id": str(report_id), "summary": summary,
            "mining": mining, "limit_anchor_refresh": limit_anchor_refresh,
            "limit_linkage_mining": linkage, "delivery": delivery}


__all__ = ["run", "summary_sections"]
