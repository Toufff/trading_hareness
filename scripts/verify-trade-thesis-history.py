"""Read-only 2026-09-15..18 trade-thesis replay acceptance.

Uses stored scans and point-in-time availability only.  It never calls the
persistence service, providers, recommendation writers, or broker surfaces.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "quant-service"
sys.path.insert(0, str(SERVICE))

from app.database import Database
from app.trade_thesis.rules import evaluate_thesis
from app.trade_thesis.scan_adapter import (
    digest,
    load_source,
    market_evidence,
    scan_candidates,
    seed_thesis,
)

SH = ZoneInfo("Asia/Shanghai")
START, END = date(2026, 9, 15), date(2026, 9, 18)


def load_env(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (None if value is None else str(value))


def replay_cutoff(source: dict[str, Any]) -> datetime:
    scan = source["scan"]
    for key in ("observed_at", "generated_at", "captured_at", "available_at"):
        if scan.get(key):
            value = datetime.fromisoformat(str(scan[key]).replace("Z", "+00:00"))
            return value if value.tzinfo else value.replace(tzinfo=SH)
    return datetime.fromisoformat(source["available_at"])


def item_states(scan: dict[str, Any]) -> dict[str, list[str]]:
    states: dict[str, set[str]] = {}
    for lane in scan.get("lanes", []):
        for key in ("selected", "observation_list"):
            for item in lane.get(key, []):
                if item.get("symbol"):
                    states.setdefault(str(item["symbol"]), set()).add(key)
    return {symbol: sorted(values) for symbol, values in states.items()}


def run(database: Database) -> dict[str, Any]:
    with database.transaction() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        runs = connection.execute("""
            SELECT DISTINCT ON (as_of_date) run_id,as_of_date,updated_at
              FROM quant.post_close_strategy_runs
             WHERE as_of_date BETWEEN %s AND %s AND status IN ('completed','partial')
               AND summary ? 'strategy_lanes'
             ORDER BY as_of_date,updated_at DESC
        """, (START, END)).fetchall()
    rows: list[dict[str, Any]] = []
    late_negative_tests: list[dict[str, Any]] = []
    totals = {"source_runs": len(runs), "candidate_rows": 0, "included": 0, "missing": 0,
              "late_rows": 0, "eligible_bars": 0, "eligible_flows": 0}
    for run_row in runs:
        source = load_source(database, str(run_row["run_id"]))
        cutoff = replay_cutoff(source)
        candidates = scan_candidates(source["scan"])
        states = item_states(source["scan"])
        targets = sorted(set(candidates) & set(states))
        totals["candidate_rows"] += len(targets)
        with database.transaction() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            sessions = connection.execute("""
                SELECT calendar_date FROM quant.market_trade_calendar
                 WHERE exchange='SSE' AND is_open AND calendar_date<=%s
                 ORDER BY calendar_date DESC LIMIT 6
            """, (source["data_date"],)).fetchall()
            next_session = connection.execute("""
                SELECT calendar_date FROM quant.market_trade_calendar
                 WHERE exchange='SSE' AND is_open AND calendar_date>%s
                 ORDER BY calendar_date LIMIT 1
            """, (source["data_date"],)).fetchone()
            days = [row["calendar_date"] for row in reversed(sessions)]
            bars = connection.execute("""
                SELECT symbol,trading_date,close,low,amount,available_at,source_observation_ids
                  FROM quant.canonical_bars_daily
                 WHERE symbol=ANY(%s) AND trading_date=ANY(%s)
                 ORDER BY symbol,trading_date,available_at
            """, (targets, days)).fetchall() if targets and days else []
            flows = connection.execute("""
                SELECT symbol,trading_date,net_amount,available_at
                  FROM quant.stock_money_flow_daily
                 WHERE symbol=ANY(%s) AND trading_date=ANY(%s) AND source='longhuvip_main_net'
                 ORDER BY symbol,trading_date,available_at
            """, (targets, days)).fetchall() if targets and days else []
        expected_days = [str(day) for day in days]
        expected_date = str(days[-1]) if days else source["data_date"]
        deadline = datetime.combine(next_session["calendar_date"], time(15), SH).isoformat() if next_session else cutoff.isoformat()
        bars_by: dict[str, list[dict[str, Any]]] = {}
        flows_by: dict[str, list[dict[str, Any]]] = {}
        for item in bars:
            bars_by.setdefault(item["symbol"], []).append(dict(item))
        for item in flows:
            flows_by.setdefault(item["symbol"], []).append(dict(item))
        for symbol in targets:
            candidate = candidates[symbol]
            symbol_bars = bars_by.get(symbol, [])
            symbol_flows = flows_by.get(symbol, [])
            eligible_bars = [bar for bar in symbol_bars if bar.get("available_at") and bar["available_at"] <= cutoff]
            late_bars = [bar for bar in symbol_bars if bar.get("available_at") and bar["available_at"] > cutoff]
            eligible_flows = [flow for flow in symbol_flows if flow.get("available_at") and flow["available_at"] <= cutoff]
            totals["eligible_bars"] += len(eligible_bars)
            totals["eligible_flows"] += len(eligible_flows)
            totals["late_rows"] += len(late_bars)
            evidence = market_evidence(symbol, symbol_bars, symbol_flows, cutoff.isoformat(),
                                       expected_date, expected_days)
            thesis = seed_thesis(candidate, source["run_id"], source["available_at"],
                                 cutoff.isoformat(), deadline, origin_mode="reconstructed")
            evaluation = evaluate_thesis(thesis, evidence, cutoff.isoformat(), None)
            present_days = {str(bar["trading_date"]) for bar in eligible_bars}
            missing_days = sorted(set(expected_days) - present_days)
            status = "included" if evidence else ("late_excluded" if late_bars else "missing_evidence")
            totals["included" if evidence else "missing"] += 1
            rows.append({
                "date": source["data_date"], "source_run_id": source["run_id"],
                "source_available_at": source["available_at"], "replay_cutoff": cutoff.isoformat(),
                "symbol": symbol, "name": candidate.get("name"), "states": states[symbol],
                "memberships": candidate["memberships"], "status": status,
                "expected_sessions": expected_days, "missing_sessions": missing_days,
                "eligible_bar_rows": len(eligible_bars), "late_bar_rows": len(late_bars),
                "eligible_flow_rows": len(eligible_flows), "evidence_count": len(evidence),
                "evaluation_id": evaluation["evaluation_id"], "input_hash": evaluation["input_hash"],
                "states_result": evaluation["states"],
            })
            if late_bars:
                without = market_evidence(symbol, eligible_bars, eligible_flows, cutoff.isoformat(),
                                          expected_date, expected_days)
                with_late = market_evidence(symbol, symbol_bars, symbol_flows, cutoff.isoformat(),
                                            expected_date, expected_days)
                before = evaluate_thesis(thesis, without, cutoff.isoformat(), None)
                after = evaluate_thesis(thesis, with_late, cutoff.isoformat(), None)
                late_negative_tests.append({
                    "date": source["data_date"], "symbol": symbol,
                    "late_available_at": [iso(bar["available_at"]) for bar in late_bars],
                    "evidence_hash_unchanged": digest(without) == digest(with_late),
                    "evaluation_unchanged": before["content_hash"] == after["content_hash"],
                })
    with database.transaction() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        bse = connection.execute("""
            SELECT count(*)::int AS bars,
                   count(*) FILTER (WHERE limit_up IS NOT NULL OR limit_down IS NOT NULL)::int AS with_limits
              FROM quant.canonical_bars_daily b JOIN quant.instruments i USING(symbol)
             WHERE i.exchange='BSE' AND b.trading_date BETWEEN %s AND %s
        """, (START, END)).fetchone()
    late_pass = bool(late_negative_tests) and all(
        item["evidence_hash_unchanged"] and item["evaluation_unchanged"] for item in late_negative_tests
    )
    return {
        "scope": {"start": str(START), "end": str(END), "mode": "read_only_in_memory",
                  "writes": False, "provider_calls": False},
        "coverage": totals, "rows": rows, "late_negative_tests": late_negative_tests,
        "late_negative_status": "passed" if late_pass else ("failed" if late_negative_tests else "not_covered_no_real_late_row"),
        "t24_bse_quote_correction": {
            "status": "not_covered_no_explicit_correction_lineage",
            "stored_bse_bars": int(bse["bars"] or 0), "stored_bse_rows_with_limits": int(bse["with_limits"] or 0),
            "note": "日线/涨跌停字段不证明真实修正版本、up-floor/down-ceil或盘中可成交性。",
        },
    }


def markdown(report: dict[str, Any]) -> str:
    c = report["coverage"]
    lines = ["# 交易假设历史只读回放验收", "", "- 范围：2026-09-15—2026-09-18",
             "- 模式：生产库只读；纯内存评价；无抓数、无落库、无推荐回填。",
             f"- 扫描轮次：{c['source_runs']}；selected/有排名 observation 去重行：{c['candidate_rows']}。",
             f"- 有可用证据：{c['included']}；缺失/晚到后无可用证据：{c['missing']}。",
             f"- 可用 bar/flow：{c['eligible_bars']}/{c['eligible_flows']}；晚到 bar：{c['late_rows']}。",
             f"- 晚到负测试：{report['late_negative_status']}。", "",
             "## 逐行纳入/排除", "",
             "| 日期 | 股票 | 原状态 | 结论 | 证据 | 缺失会话 | 晚到行 |", "|---|---|---|---|---:|---|---:|"]
    for row in report["rows"]:
        lines.append(f"| {row['date']} | {row['name']}（{row['symbol']}） | {','.join(row['states'])} | "
                     f"{row['status']} | {row['evidence_count']} | {','.join(row['missing_sessions']) or '-'} | {row['late_bar_rows']} |")
    lines += ["", "## T24 北交所报价修正", "",
              f"- 状态：{report['t24_bse_quote_correction']['status']}",
              f"- 区间内北交所 bar：{report['t24_bse_quote_correction']['stored_bse_bars']}；带涨跌停字段：{report['t24_bse_quote_correction']['stored_bse_rows_with_limits']}。",
              f"- {report['t24_bse_quote_correction']['note']}", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(r"G:\StockPlatform\config\runtime.env"))
    parser.add_argument("--output", type=Path,
                        default=Path(r"G:\StockPlatform\reports\reviews\thesis-live-acceptance"))
    args = parser.parse_args()
    load_env(args.env_file)
    report = run(Database())
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "historical-replay.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8",
    )
    (args.output / "historical-replay.md").write_text(markdown(report) + "\n", encoding="utf-8")
    print(json.dumps({"status": "completed", "coverage": report["coverage"],
                      "late_negative_status": report["late_negative_status"],
                      "t24": report["t24_bse_quote_correction"]["status"]}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
