"""Prepare an offline, private B/S review for one imported stock/day.

No broker UI, trading, public publishing, database writes or background jobs.
"""

from __future__ import annotations

import argparse
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
    parser.add_argument("--symbol", required=True, help="6 位代码或 600664.SH")
    parser.add_argument("--account-key", default="citics-primary")
    parser.add_argument("--env-file", type=Path, default=Path(r"G:\StockPlatform\config\runtime.env"))
    parser.add_argument("--output-root", type=Path, default=Path(r"G:\StockPlatform\reports\stock-sb-review"))
    parser.add_argument("--echarts-js", type=Path,
                        default=ROOT / "frontend" / "node_modules" / "echarts" / "dist" / "echarts.min.js")
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
    from app.stock_sb_review import collect_review, normalized_symbol
    from app.stock_sb_review_page import write_review_page

    try:
        symbol = normalized_symbol(args.symbol)
        database = Database()
        try:
            with database.transaction(statement_timeout_ms=45_000) as connection:
                payload = collect_review(connection, account_key=args.account_key,
                                         day=args.date, symbol=symbol)
        finally:
            database.close()
        payload["generated_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        out = args.output_root / args.account_key / f"{args.date}_{symbol}"
        page = write_review_page(payload, out, args.echarts_js)
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        (out / "evidence.json").write_bytes(raw)
        digest = sha256(raw).hexdigest()
        (out / "manifest.json").write_text(json.dumps({
            "schema": "stock-sb-review-v1", "generated_at": payload["generated_at"],
            "evidence_sha256": digest, "day": args.date.isoformat(), "symbol": symbol,
            "account_key": args.account_key, "events": len(payload["events"]),
            "executions": sum(bool(x["is_execution"]) for x in payload["events"]),
            "gaps": payload["coverage"]["gaps"], "page": str(page),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "prepared", "page": str(page), "evidence_sha256": digest,
                          "events": len(payload["events"]), "executions": sum(bool(x["is_execution"]) for x in payload["events"]),
                          "minute_bars": payload["coverage"]["stock_minute_bars"],
                          "gaps": payload["coverage"]["gaps"]}, ensure_ascii=False))
        return 0
    except (ValueError, FileNotFoundError) as exc:
        print(json.dumps({"status": "unavailable", "reason": str(exc)}, ensure_ascii=False))
        return 2
    except Exception as exc:
        # Do not emit DSNs, SQL details or account secrets in the CLI result.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}, ensure_ascii=False))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
