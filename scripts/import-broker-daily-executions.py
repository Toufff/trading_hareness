"""Import a date-less THS 当日成交 text export with an explicit date and account attribution."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quant-service"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--trade-date", type=date.fromisoformat, required=True)
    parser.add_argument("--account-key", required=True)
    parser.add_argument("--broker", required=True)
    parser.add_argument("--confirm-account-attribution", action="store_true",
                        help="The user explicitly associates this date-less, account-less export with this account")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--finalize-source", action="store_true")
    parser.add_argument("--archive-root", type=Path,
                        default=Path(r"G:\StockPlatform\data\imports\broker-trades\archive"))
    parser.add_argument("--inbox-root", type=Path,
                        default=Path(r"G:\StockPlatform\data\imports\broker-trades\inbox"))
    parser.add_argument("--processed-root", type=Path,
                        default=Path(r"G:\StockPlatform\data\imports\broker-trades\processed"))
    parser.add_argument("--env-file", type=Path, default=Path(r"G:\StockPlatform\config\runtime.env"))
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from app.broker_export_parser import parse_daily_execution_export
    from app.broker_daily_execution_import import finalize_daily_execution_source, import_daily_execution_export
    try:
        parsed = parse_daily_execution_export(args.input.resolve(), args.trade_date)
        if args.validate_only:
            if args.finalize_source:
                parser.error("--validate-only and --finalize-source cannot be combined")
            print(json.dumps({"status": "valid", "source_sha256": parsed.sha256,
                              "trade_date": args.trade_date.isoformat(),
                              "source_rows": len(parsed.rows) + len(parsed.ignored_rows),
                              "fill_rows": len(parsed.rows),
                              "zero_or_ignored_rows": len(parsed.ignored_rows),
                              "account_attribution": "explicit_confirmation_required;file_has_no_account_or_date"},
                             ensure_ascii=False))
            return 0
        if not args.confirm_account_attribution:
            raise ValueError("BROKER_DAILY_EXECUTION_ACCOUNT_ATTRIBUTION_REQUIRED")
        from dotenv import load_dotenv
        load_dotenv(args.env_file, override=True)
        from app.database import Database
        database = Database()
        try:
            with database.transaction(statement_timeout_ms=30_000) as connection:
                result = import_daily_execution_export(
                    connection, args.input, day=args.trade_date, account_key=args.account_key,
                    broker=args.broker, archive_root=args.archive_root,
                    confirmed_account_attribution=True,
                )
        finally:
            database.close()
        if args.finalize_source:
            result.update(finalize_daily_execution_source(
                args.input, parsed, day=args.trade_date, inbox_root=args.inbox_root,
                processed_root=args.processed_root,
            ))
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except Exception as error:
        print(json.dumps({"status": "failed", "error_code": str(error).split(":", 1)[0]}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
