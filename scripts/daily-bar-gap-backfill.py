#!/usr/bin/env python3
"""Fill-only backfill of missing A-share daily bars from licensed longhu history.

The executable wrapper for ``app.daily_bar_gap_backfill`` (read its module
docstring for the source, the validation and what is written).

Subcommands (all take ``--from/--to`` and ``--evidence-dir``):

``fetch``     read-only on the database; pulls the vendor kline for every
              symbol of the universe and the dated L2 snapshot for every
              (symbol, traded session) still missing from canonical, into the
              evidence directory.  Resumable: cached evidence is not fetched again.
``validate``  read-only: snapshots for stored sessions (``--dates``) compared
              with the canonical rows already in the database -- the method's
              acceptance test.  Fetches what it needs into the evidence dir.
``plan``      read-only (server-enforced ``default_transaction_read_only``):
              the rows that would be inserted per session, everything held
              back with its reason, and every continuity flag.
``apply``     writes: one short transaction per session, ``ON CONFLICT DO
              NOTHING`` everywhere, then reads every session back.

stdout is ASCII-only JSON.  No credential value is ever printed; the env file
is loaded into ``os.environ`` and nothing reads it back out.

Exit codes: 0 ok; 1 a session was refused (coverage) or a readback failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quant-service"))

DEFAULT_ENV_FILE = r"G:\StockPlatform\config\runtime.env"
PROXY_VARIABLES = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("fetch", "validate", "plan", "apply"):
        command = commands.add_parser(name)
        command.add_argument("--from", dest="from_date", type=date.fromisoformat, required=True)
        command.add_argument("--to", dest="to_date", type=date.fromisoformat, required=True)
        command.add_argument("--evidence-dir", required=True)
        command.add_argument("--env-file", default=DEFAULT_ENV_FILE)
        command.add_argument("--workers", type=int, default=16)
        command.add_argument("--report", default=None, help="write the full JSON report here")
        command.add_argument("--database", default=None,
                             help="override PGDATABASE after loading the env file (rehearsal on a scratch copy)")
        if name == "validate":
            command.add_argument("--dates", required=True,
                                 help="comma-separated stored sessions to compare (YYYY-MM-DD)")
            command.add_argument("--symbols", default=None, help="optional comma-separated subset")
        if name == "apply":
            command.add_argument("--run-id", default=None)
            command.add_argument("--lock-timeout-ms", type=int, default=3000)
    return parser.parse_args(argv)


def load_env_file(path: str) -> int:
    loaded = 0
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()
        loaded += 1
    return loaded


def clear_proxies() -> None:
    for name in PROXY_VARIABLES:
        os.environ.pop(name, None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"


@contextmanager
def connection(*, read_only: bool, application_name: str = "daily-bar-gap-backfill"):
    import psycopg
    from psycopg.rows import dict_row

    from app.db_dsn import connection_params

    options = "-c statement_timeout=600000"
    if read_only:
        options += " -c default_transaction_read_only=on"
    with psycopg.connect(**connection_params(), row_factory=dict_row, connect_timeout=10,
                         options=options, application_name=application_name) as conn:
        yield conn


def emit(payload: dict, report: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    if report:
        Path(report).write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True, default=str),
                                encoding="utf-8")
    print(text if len(text) < 20000 else json.dumps({k: payload[k] for k in payload if k != "detail"},
                                                     ensure_ascii=True, sort_keys=True, default=str))


def fetch_evidence(store, source, pairs: list[tuple[str, str]], symbols: list[str], workers: int) -> dict:
    from app import daily_bar_gap_backfill as backfill

    kline_todo = [symbol for symbol in symbols if not store.has_kline(symbol)]
    kline_errors = backfill.fetch_parallel(
        kline_todo, lambda symbol: backfill.fetch_kline_record(source, symbol), workers=workers,
        on_result=lambda _symbol, record: store.put_kline(record))
    cached: dict[str, set[str]] = {}
    todo = []
    for symbol, day in pairs:
        if day not in cached:
            cached[day] = set(store.pankou_day(day))
        if symbol not in cached[day]:
            todo.append((symbol, day))
    buffer: list[dict] = []

    def keep(_item, record):
        buffer.append(record)
        if len(buffer) >= 2000:
            store.append_pankou(buffer[:])
            del buffer[:]

    pankou_errors = backfill.fetch_parallel(
        todo, lambda item: backfill.fetch_pankou_record(source, item[0], item[1]), workers=workers, on_result=keep)
    store.append_pankou(buffer)
    return {"kline_fetched": len(kline_todo) - len(kline_errors), "kline_errors": dict(list(kline_errors.items())[:20]),
            "kline_failed": len(kline_errors), "pankou_fetched": len(todo) - len(pankou_errors),
            "pankou_failed": len(pankou_errors),
            "pankou_errors": {f"{s} {d}": e for (s, d), e in list(pankou_errors.items())[:20]}}


def window_pairs(conn, store, from_date: date, to_date: date) -> tuple[list[str], list[tuple[str, str]]]:
    from datetime import timedelta

    from app import daily_bar_gap_backfill as backfill

    universe = [row["symbol"] for row in conn.execute(backfill.UNIVERSE_SQL, {
        "before": from_date - timedelta(days=30), "after": to_date + timedelta(days=30),
        "from_date": from_date, "to_date": to_date}).fetchall() if backfill.in_scope(row["symbol"])]
    sessions = {row["calendar_date"] for row in conn.execute(
        backfill.OPEN_SESSIONS_SQL, (from_date, to_date)).fetchall()}
    existing = {(row["symbol"], row["trading_date"]) for row in conn.execute(
        backfill.EXISTING_KEYS_SQL, (from_date, to_date)).fetchall()}
    return universe, [(symbol, value.strftime("%Y%m%d")) for symbol in universe if not backfill.is_index(symbol)
                      for value in sorted(backfill.parse_kline(store.kline(symbol)))
                      if value in sessions and (symbol, value) not in existing]


def command_fetch(args) -> int:
    from app import daily_bar_gap_backfill as backfill
    from app.longhu_vendor_source import intraday_source

    store = backfill.EvidenceStore(args.evidence_dir)
    source = intraday_source()
    with connection(read_only=True) as conn:
        universe, _ = window_pairs(conn, store, args.from_date, args.to_date)
    kline_only = fetch_evidence(store, source, [], universe, args.workers)
    with connection(read_only=True) as conn:
        universe, pairs = window_pairs(conn, store, args.from_date, args.to_date)
    result = fetch_evidence(store, source, pairs, universe, args.workers)
    emit({"command": "fetch", "universe": len(universe), "pairs": len(pairs),
          "kline": kline_only, **result}, args.report)
    return 0 if not result["pankou_failed"] and not kline_only["kline_failed"] else 1


def command_validate(args) -> int:
    from collections import Counter

    from app import daily_bar_gap_backfill as backfill
    from app.longhu_vendor_source import intraday_source

    store = backfill.EvidenceStore(args.evidence_dir)
    dates = [date.fromisoformat(value) for value in args.dates.split(",") if value.strip()]
    wanted = set(args.symbols.split(",")) if args.symbols else None
    with connection(read_only=True) as conn:
        stored = conn.execute(
            f"""SELECT symbol,trading_date,open,high,low,close,pre_close,volume,amount,limit_up,limit_down
                  FROM quant.canonical_bars_daily
                 WHERE trading_date = ANY(%s) AND symbol ~ '{backfill._A_SHARE_PATTERN}'""",
            (dates,)).fetchall()
    stored = [row for row in stored if not backfill.is_index(row["symbol"])
              and (wanted is None or row["symbol"] in wanted)]
    pairs = [(row["symbol"], row["trading_date"].strftime("%Y%m%d")) for row in stored]
    fetched = fetch_evidence(store, intraday_source(), pairs, sorted({s for s, _ in pairs}), args.workers)
    counts: Counter = Counter()
    mismatches: dict[str, list] = {}
    for row in stored:
        day = row["trading_date"].strftime("%Y%m%d")
        record = store.pankou_day(day).get(row["symbol"])
        bar, reason = backfill.parse_pankou_record(record or {}, row["symbol"], row["trading_date"])
        counts["compared"] += 1
        if bar is None:
            counts[f"held_{reason}"] += 1
            continue
        exact = True
        for name, mine, theirs in (("open", bar.open, row["open"]), ("high", bar.high, row["high"]),
                                   ("low", bar.low, row["low"]), ("close", bar.close, row["close"]),
                                   ("pre_close", bar.pre_close, row["pre_close"])):
            if theirs is None or mine != Decimal(str(theirs)).quantize(backfill.TICK):
                exact = False
                counts[f"{name}_mismatch"] += 1
                mismatches.setdefault(name, []).append((row["symbol"], day, str(mine), str(theirs)))
        counts["ohlc_pre_close_exact" if exact else "ohlc_pre_close_mismatch"] += 1
        if row["volume"] is not None and abs(bar.volume - Decimal(str(row["volume"]))) >= 1:
            counts["volume_mismatch"] += 1
            mismatches.setdefault("volume", []).append((row["symbol"], day, str(bar.volume), str(row["volume"])))
        if row["amount"] is not None and abs(bar.amount - Decimal(str(row["amount"]))) > max(
                Decimal("0.01"), Decimal(str(row["amount"])) * Decimal("1e-6")):
            counts["amount_mismatch"] += 1
            mismatches.setdefault("amount", []).append((row["symbol"], day, str(bar.amount), str(row["amount"])))
        if row["limit_up"] is not None and Decimal(str(row["limit_up"])) < Decimal("9999"):
            if bar.limit_up != Decimal(str(row["limit_up"])).quantize(backfill.TICK) or \
                    bar.limit_down != Decimal(str(row["limit_down"])).quantize(backfill.TICK):
                counts["limit_mismatch"] += 1
                mismatches.setdefault("limit", []).append(
                    (row["symbol"], day, str(bar.limit_up), str(bar.limit_down),
                     str(row["limit_up"]), str(row["limit_down"])))
            else:
                counts["limit_exact"] += 1
    emit({"command": "validate", "dates": [str(value) for value in dates], "fetch": fetched,
          "counts": dict(counts), "mismatch_samples": {k: v[:20] for k, v in mismatches.items()},
          "detail": mismatches}, args.report)
    return 0


def plan_payload(plan) -> dict:
    return {"from": str(plan.from_date), "to": str(plan.to_date), "stats": plan.stats,
            "coverage": {str(value): round(__import__("app.daily_bar_gap_backfill", fromlist=["x"])
                                           .session_coverage(plan, value), 5) for value in plan.sessions},
            "held_samples": {reason: items[:20] for reason, items in plan.held.items()},
            "flag_samples": {name: items[:20] for name, items in plan.flags.items()},
            "detail": {"held": plan.held, "flags": plan.flags}}


def command_plan(args) -> int:
    from app import daily_bar_gap_backfill as backfill

    store = backfill.EvidenceStore(args.evidence_dir)
    with connection(read_only=True) as conn:
        plan = backfill.build_plan(conn, store, args.from_date, args.to_date)
    emit({"command": "plan", **plan_payload(plan)}, args.report)
    return 0


READBACK_SQL = """SELECT trading_date, count(*) FILTER (WHERE selected_provider = ANY(%s)) AS written,
       count(*) FILTER (WHERE symbol ~ '^[0-9]{6}[.](SH|SZ|BJ)$') AS a_share
  FROM quant.canonical_bars_daily WHERE trading_date BETWEEN %s AND %s GROUP BY 1 ORDER BY 1"""


def command_apply(args) -> int:
    from app import daily_bar_gap_backfill as backfill

    store = backfill.EvidenceStore(args.evidence_dir)
    run_id = args.run_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    with connection(read_only=True) as conn:
        plan = backfill.build_plan(conn, store, args.from_date, args.to_date)
    results: dict[str, dict] = {}
    refused: dict[str, float] = {}
    for value in plan.sessions:
        bars = plan.rows[value]
        coverage = backfill.session_coverage(plan, value)
        if coverage < backfill.MIN_SESSION_COVERAGE:
            refused[str(value)] = coverage
            continue
        with connection(read_only=False) as conn:
            with conn.transaction():
                conn.execute(f"SET LOCAL lock_timeout = '{int(args.lock_timeout_ms)}ms'")
                conn.execute("SET LOCAL statement_timeout = '120s'")
                results[str(value)] = backfill.persist_session(conn, value, bars, plan.evidence, run_id=run_id)
        print(json.dumps({"session": str(value), **results[str(value)]}, ensure_ascii=True), flush=True)
    with connection(read_only=True) as conn:
        readback = {str(row["trading_date"]): {"written": row["written"], "a_share": row["a_share"]}
                    for row in conn.execute(READBACK_SQL, ([backfill.PROVIDER_KEY, backfill.INDEX_PROVIDER_KEY],
                                                           args.from_date, args.to_date)).fetchall()}
    short = {day: {"planned": len(plan.rows[date.fromisoformat(day)]), "readback": readback.get(day)}
             for day in results
             if (readback.get(day) or {}).get("written", 0) < results[day]["canonical"]}
    emit({"command": "apply", "run_id": run_id, "results": results, "refused": refused,
          "readback": readback, "readback_short": short, "stats": plan.stats}, args.report)
    return 1 if refused or short else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env_file(args.env_file)
    if args.database:
        os.environ["PGDATABASE"] = args.database
    clear_proxies()
    return {"fetch": command_fetch, "validate": command_validate, "plan": command_plan,
            "apply": command_apply}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
