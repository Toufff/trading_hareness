"""Read-only three-arm trade-thesis effectiveness receipt.

Without ``--input`` the owner database can currently prove captured theses and
evaluations, but not historical decisions for all three arms.  Those rows are
therefore emitted with explicit unavailable reasons, never backfilled into
synthetic trades.  ``--input`` is for frozen fixtures/replay exports only.
"""
import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quant-service"))
DEFAULT_OUTPUT = Path("G:/StockPlatform/reports/reviews/thesis-live-acceptance")


def read_owner(database, limit):
    with database.transaction() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        runs = connection.execute("""
            SELECT run_id,as_of_date,updated_at,summary FROM quant.post_close_strategy_runs
             WHERE status='completed' ORDER BY as_of_date DESC,updated_at DESC LIMIT %s
        """, (limit,)).fetchall()
        rows = connection.execute("""
            SELECT r.thesis_id,r.symbol,r.payload,r.created_at,
                   e.evaluation_id,e.cutoff_at,e.result
              FROM quant.trade_thesis_revisions r
              LEFT JOIN LATERAL (
                SELECT evaluation_id,cutoff_at,result FROM quant.trade_thesis_evaluations
                 WHERE thesis_id=r.thesis_id AND namespace='shadow'
                 ORDER BY cutoff_at DESC,created_at DESC LIMIT 1
              ) e ON TRUE
             WHERE r.event_type IN ('capture','approve')
             ORDER BY r.created_at DESC LIMIT %s
        """, (limit,)).fetchall()
    snapshot_records = []
    for run in runs:
        summary = run["summary"] or {}
        thesis = summary.get("trade_thesis") or (summary.get("strategy_lanes") or {}).get("trade_thesis") or {}
        snapshot = thesis.get("three_arm_snapshot") or summary.get("trade_thesis_three_arm_snapshot")
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("arms"), dict):
            continue
        by_arm = {arm: {str(item["symbol"]): item for item in snapshot["arms"].get(arm, []) if item.get("symbol")}
                  for arm in ("current_baseline", "pure_machine", "lifecycle")}
        for symbol in sorted(set().union(*(set(items) for items in by_arm.values()))):
            decisions = {}
            for arm, items in by_arm.items():
                item = items.get(symbol)
                if item is None:
                    decisions[arm] = {"selected": False, "exclude_reason": "decision_record_missing"}
                else:
                    decisions[arm] = {key: item.get(key) for key in (
                        "selected", "exclude_reason", "eligibility", "action", "rankings",
                        "execution_policy", "fill_contract"
                    ) if key in item}
            snapshot_records.append({"record_id": f"{run['run_id']}:{symbol}",
                "cohort_id": str(snapshot.get("content_hash") or run["run_id"]), "symbol": symbol,
                "event_id": str(next((item.get("origin_id") or item.get("thesis_id") for items in by_arm.values()
                                       for item in [items.get(symbol)] if item), f"{run['run_id']}:{symbol}")),
                "signal_date": str(run["as_of_date"]), "available_at": str(snapshot.get("captured_at") or run["updated_at"].isoformat()),
                "decisions": decisions, "snapshot_hash": snapshot.get("content_hash")})
    if snapshot_records:
        # The evaluator is intentionally one frozen cohort per invocation. Use
        # the newest persisted snapshot; later CLI work can fan out cohorts.
        newest = snapshot_records[0]["cohort_id"]
        return [record for record in snapshot_records if record["cohort_id"] == newest]
    records = []
    for row in rows:
        payload = row["payload"] or {}
        created = row["created_at"]
        event_id = str(payload.get("primary_origin_id") or row["thesis_id"])
        records.append({
            "record_id": str(row["thesis_id"]), "cohort_id": "owner_legacy_history_unavailable",
            "symbol": row["symbol"], "event_id": event_id,
            "signal_date": str(payload.get("effective_from") or created)[:10], "available_at": created.isoformat(),
            "decisions": {
                arm: {"selected": False, "exclude_reason": "historical_arm_decision_unavailable"}
                for arm in ("current_baseline", "pure_machine", "lifecycle")
            },
            "source_refs": {"thesis_id": str(row["thesis_id"]), "evaluation_id": str(row["evaluation_id"]) if row["evaluation_id"] else None},
        })
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--train-end", type=date.fromisoformat)
    parser.add_argument("--holdout-start", type=date.fromisoformat)
    parser.add_argument("--minimum-holdout", type=int, default=20)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--env-file", default="G:/StockPlatform/config/runtime.env")
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 5000:
        parser.error("--limit must be between 1 and 5000")
    holdout_start = args.holdout_start or args.as_of - timedelta(days=90)
    train_end = args.train_end or holdout_start - timedelta(days=1)
    if args.input:
        records = json.loads(args.input.read_text(encoding="utf-8"))
        source = {"kind": "frozen_input", "path": str(args.input)}
    else:
        from dotenv import load_dotenv
        from app.database import Database
        load_dotenv(args.env_file, override=True)
        database = Database()
        try:
            records = read_owner(database, args.limit)
        finally:
            database.close()
        source = {"kind": "owner_read_only", "rows": len(records),
                  "limitation": "three-arm historical decision and executable fill contracts are not persisted for legacy rows"}
    from app.trade_thesis.effectiveness import evaluate_three_arms
    result = evaluate_three_arms(records, train_end=str(train_end), holdout_end=str(args.as_of),
                                 minimum_holdout=args.minimum_holdout)
    result["source"] = source
    result["real_profitability_validation"] = False
    result["claim"] = "engineering/exposure receipt only; unavailable history is not evidence of zero return"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    output = args.output_dir / f"three-arm-{args.as_of.isoformat()}-{stamp}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"status": result["status"], "output": str(output), "events": result["events"],
                      "paired": result["paired"]["count"], "real_profitability_validation": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
