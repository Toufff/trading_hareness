"""Prepare an offline, private B/S review for one imported stock/day.

No broker UI, trading, public publishing, database writes or background jobs.
Index and stock minute tapes missing from the database may be fetched once from
the licensed Longhu minute endpoint; they are embedded only when the provider
declares the requested session date and are never persisted.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo
from datetime import datetime


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quant-service"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, required=True, help="交易日 YYYY-MM-DD")
    parser.add_argument("--symbol", required=True, help="股票名称、6 位代码或 600664.SH")
    parser.add_argument("--account-key", default="citics-primary")
    parser.add_argument("--env-file", type=Path, default=Path(r"G:\StockPlatform\config\runtime.env"))
    parser.add_argument("--output-root", type=Path, default=Path(r"G:\StockPlatform\reports\stock-sb-review"))
    parser.add_argument("--echarts-js", type=Path,
                        default=ROOT / "frontend" / "node_modules" / "echarts" / "dist" / "echarts.min.js")
    parser.add_argument("--sector", action="append", default=[],
                        help="人工指定的关注板块名（可重复）；系统无归属数据时仅作对照，不视为归属事实")
    parser.add_argument("--benchmark-minutes", choices=("auto", "off"), default="auto",
                        help="auto: 数据库缺指数或个股分钟线时，按日期校验后从开盘啦分钟接口补采（不入库）")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    if args.date > today:
        parser.error("复盘交易日不能晚于今天")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", args.account_key):
        parser.error("账户键格式无效")
    if not args.env_file.is_file():
        parser.error("运行配置文件不存在")

    from dotenv import load_dotenv
    load_dotenv(args.env_file, override=True)
    from app.database import Database
    from app.stock_sb_review import collect_review, resolve_order_symbol
    from app.stock_sb_review_page import write_review_page

    try:
        database = Database()
        try:
            with database.transaction(statement_timeout_ms=45_000) as connection:
                symbol = resolve_order_symbol(connection, account_key=args.account_key,
                                              day=args.date, stock=args.symbol)
            # Fetch outside the transaction; a stock tape is only used when the database has none.
            external = fetch_minutes(args.date, symbol) if args.benchmark_minutes == "auto" else {}
            external_stock = external.pop(symbol, None)
            with database.transaction(statement_timeout_ms=45_000) as connection:
                payload = collect_review(connection, account_key=args.account_key,
                                         day=args.date, symbol=symbol, pinned_sectors=args.sector,
                                         external_benchmarks=external, external_stock_minutes=external_stock)
        finally:
            database.close()
        payload["generated_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        out = args.output_root / args.account_key / f"{args.date}_{symbol}"
        page = write_review_page(payload, out, args.echarts_js)
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        (out / "evidence.json").write_bytes(raw)
        digest = sha256(raw).hexdigest()
        (out / "manifest.json").write_text(json.dumps({
            "schema": "stock-sb-review-v2", "generated_at": payload["generated_at"],
            "evidence_sha256": digest, "day": args.date.isoformat(), "symbol": symbol,
            "account_key": args.account_key, "events": len(payload["events"]),
            "executions": sum(bool(x["is_execution"]) for x in payload["events"]),
            "coverage": {key: value for key, value in payload["coverage"].items() if key != "gaps"},
            "gaps": payload["coverage"]["gaps"], "page": str(page),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "prepared", "page": str(page), "evidence_sha256": digest,
                          "events": len(payload["events"]), "executions": sum(bool(x["is_execution"]) for x in payload["events"]),
                          "minute_bars": payload["coverage"]["stock_minute_bars"],
                          "coverage": {key: value for key, value in payload["coverage"].items() if key != "gaps"},
                          "gaps": payload["coverage"]["gaps"]}, ensure_ascii=False))
        return 0
    except (ValueError, FileNotFoundError) as exc:
        print(json.dumps({"status": "unavailable", "reason": str(exc)}, ensure_ascii=False))
        return 2
    except Exception as exc:
        # Do not emit DSNs, SQL details or account secrets in the CLI result.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}, ensure_ascii=False))
        raise


def fetch_minutes(day: date, stock: str) -> dict[str, dict]:
    """Bounded, date-checked review-time fetch; any failure becomes a visible status."""
    from app.longhu_market_data import longhu_intraday_minute_session
    from app.stock_sb_review import BENCHMARKS
    from app.stock_sb_review_context import benchmark_bars_from_session

    async def one(symbol: str) -> tuple[str, dict]:
        try:
            session = await asyncio.wait_for(longhu_intraday_minute_session(symbol), timeout=20)
        except Exception as exc:  # noqa: BLE001 - a benchmark gap must not block the review
            return symbol, {"bars": [], "source": f"fetch_failed_{type(exc).__name__}"}
        fetched_at = datetime.now(ZoneInfo("Asia/Shanghai"))
        bars, status = benchmark_bars_from_session(session, symbol=symbol, day=day, fetched_at=fetched_at)
        return symbol, {"bars": bars, "source": status}

    async def run() -> dict[str, dict]:
        return dict(await asyncio.gather(*(one(symbol) for symbol in dict.fromkeys([*BENCHMARKS, stock]))))

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
