"""Import a user-exported THS order-query text table; never controls a broker UI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quant-service"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--account-key", required=True)
    parser.add_argument("--broker", required=True)
    parser.add_argument("--confirm-account-binding", action="store_true",
                        help="Use only when the current user explicitly maps this exported account to account-key")
    parser.add_argument("--validate-only", action="store_true",
                        help="Parse and report safe metadata without writing the database or archive")
    parser.add_argument("--archive-root", type=Path,
                        default=Path(r"G:\StockPlatform\data\imports\broker-orders\archive"))
    parser.add_argument("--env-file", type=Path, default=Path(r"G:\StockPlatform\config\runtime.env"))
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from dotenv import load_dotenv
    load_dotenv(args.env_file, override=True)
    from app.broker_order_export_parser import parse_order_export
    if args.validate_only:
        parsed = parse_order_export(args.input.resolve())
        print(json.dumps({
            "status": "valid", "source_sha256": parsed.sha256,
            "masked_account": parsed.masked_account, "row_count": len(parsed.events),
            "execution_count": len(parsed.executions), "ignored_count": len(parsed.ignored_rows),
            "min_order_date": parsed.min_order_date.isoformat(),
            "max_order_date": parsed.max_order_date.isoformat(),
            "time_semantics": "order_time_proxy_until_exact_trade_export_is_available",
        }, ensure_ascii=False))
        return 0
    from app.database import Database
    from app.broker_order_repository import import_order_export
    database = Database()
    try:
        with database.transaction(statement_timeout_ms=30_000) as connection:
            result = import_order_export(
                connection, args.input, account_key=args.account_key, broker=args.broker,
                archive_root=args.archive_root, confirm_account_binding=args.confirm_account_binding,
            )
    except Exception as error:
        print(json.dumps({"status": "failed", "error_code": str(error).split(":", 1)[0]}, ensure_ascii=False))
        return 2
    finally:
        database.close()
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
