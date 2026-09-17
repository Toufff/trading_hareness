"""Store the owner's own review statements (intent, plans) in quant.personal_journal_entries.

Input is a JSONL capture of what the owner said during a review session, one
statement per line (``kind=user_statement``).  Statements for the same symbol
and day merge into one entry; a later ``next_day_plan`` supersedes an earlier
one while every raw statement stays in ``metadata.statements``.  Re-running
with the same file is idempotent.  These rows are the owner's words, not system
inferences, and never feed a strategy.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quant-service"))

SOURCE = "human_review_session"


def merge_statements(statements: list[dict], trade_date: date) -> list[dict]:
    by_symbol: "OrderedDict[str, list[dict]]" = OrderedDict()
    for row in statements:
        if row.get("kind") != "user_statement" or row.get("trade_date") != trade_date.isoformat():
            continue
        symbol = str(row.get("symbol") or "").upper()
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
            raise ValueError(f"statement without a valid symbol: {row}")
        by_symbol.setdefault(symbol, []).append(row)
    entries = []
    for symbol, rows in by_symbol.items():
        name = next((r["name"] for r in rows if r.get("name")), symbol)
        intents = [r["intent"] for r in rows if r.get("intent")]
        plans = [r["next_day_plan"] for r in rows if r.get("next_day_plan")]
        fills = [r["linked_fills"] for r in rows if r.get("linked_fills")]
        extras = {key: r[key] for r in rows for key in ("thesis_for_remaining",) if r.get(key)}
        signals = [s for r in rows for s in r.get("signals_outside_page") or []]
        body = "\n".join(intents + [f"留仓理由：{extras['thesis_for_remaining']}"] if extras else intents)
        entry = {
            "entry_date": trade_date, "entry_type": "trade", "title": f"{name} {symbol}", "body": body,
            "actions": [{**fill, "intent": intents[-1] if intents else None} for fill in fills],
            "plans": [{"for": "next_trading_day", "plan": plans[-1], "superseded": plans[:-1]}] if plans else [],
            "source_record_key": f"citics-primary:{trade_date.isoformat()}:{symbol}",
            "metadata": {"account_key": "citics-primary", "symbol": symbol, "name": name,
                         "semantics": "owner_statement_not_system_inference",
                         "signals_outside_system": signals, "statements": rows},
        }
        canonical = json.dumps({k: entry[k] for k in ("title", "body", "actions", "plans", "metadata")},
                               ensure_ascii=False, sort_keys=True, default=str)
        entry["content_hash"] = sha256(canonical.encode("utf-8")).hexdigest()
        entries.append(entry)
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(r"G:\StockPlatform\config\runtime.env"))
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    statements = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
    entries = merge_statements(statements, args.date)

    from dotenv import load_dotenv
    load_dotenv(args.env_file, override=True)
    from psycopg.types.json import Jsonb
    from app.database import Database

    database = Database()
    written = unchanged = 0
    try:
        with database.transaction() as connection:
            for entry in entries:
                current = connection.execute(
                    "SELECT content_hash FROM quant.personal_journal_entries WHERE source=%s AND source_record_key=%s",
                    (SOURCE, entry["source_record_key"]),
                ).fetchall()
                if [row["content_hash"] for row in current] == [entry["content_hash"]]:
                    unchanged += 1
                    continue
                # One current row per symbol/day: a merged restatement replaces the previous merge.
                connection.execute("DELETE FROM quant.personal_journal_entries WHERE source=%s AND source_record_key=%s",
                                   (SOURCE, entry["source_record_key"]))
                connection.execute(
                    """INSERT INTO quant.personal_journal_entries(
                           entry_date,entry_type,title,body,actions,plans,source,source_record_key,content_hash,metadata)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (entry["entry_date"], entry["entry_type"], entry["title"], entry["body"], Jsonb(entry["actions"]),
                     Jsonb(entry["plans"]), SOURCE, entry["source_record_key"], entry["content_hash"],
                     Jsonb(entry["metadata"])),
                )
                written += 1
    finally:
        database.close()
    print(json.dumps({"status": "ok", "date": args.date.isoformat(), "entries": len(entries),
                      "written": written, "unchanged": unchanged,
                      "symbols": [e["metadata"]["symbol"] for e in entries]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
