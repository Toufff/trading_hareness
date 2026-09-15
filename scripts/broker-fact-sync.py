"""Explicitly authorized manual desktop holdings import. Never operates a UI."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quant-service"))
from app.broker_manual_sync import (TASK_KEY, MAX_RUN_AGE, validate_manual_request, start_manual,
                                    complete_manual, alert_path, confirm_manual_account)
from app.broker_desktop_evidence import prepare_export_envelope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "prepare-export", "complete", "confirm-account", "fail", "status", "retry-import"])
    parser.add_argument("--phase", default="manual", help="Only manual is accepted; scheduled phases are retired")
    parser.add_argument("--manual-user-authorized", action="store_true")
    parser.add_argument("--account-key")
    parser.add_argument("--run-id")
    parser.add_argument("--envelope", type=Path)
    parser.add_argument("--confirmation-record", type=Path,
                        help="Actual current-user confirmation JSON, never an agent-authored consent statement")
    parser.add_argument("--session-manifest", type=Path,
                        help="Observation/account-binding metadata; never supplies balances, positions or trades")
    parser.add_argument("--holdings-export", type=Path)
    parser.add_argument("--account-export", type=Path)
    parser.add_argument("--trade-export", type=Path)
    parser.add_argument("--trade-batch", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--error-code", default="BROKER_SYNC_FAILED")
    parser.add_argument("--message", default="")
    parser.add_argument("--env-file", type=Path, default=Path(r"G:\StockPlatform\config\runtime.env"))
    parser.add_argument("--evidence-root", type=Path, default=Path(r"G:\StockPlatform\data\broker-evidence"))
    parser.add_argument("--base-url", default="http://127.0.0.1:5681")
    parser.add_argument("--adapter-url", default="http://127.0.0.1:5680")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    run = None
    try:
        if args.phase != "manual":
            validate_manual_request(args.phase, False, None)
        if args.action not in {"status", "fail"}:
            validate_manual_request(args.phase, args.manual_user_authorized, args.account_key)
        import psycopg
        from psycopg.rows import dict_row
        from app.db_dsn import connection_params
        from app.automation_run_repository import fail_run
        config = dict(line.split("=", 1) for line in args.env_file.read_text(encoding="utf-8-sig").splitlines()
                      if "=" in line and not line.startswith("#"))
        for key, value in config.items():
            os.environ.setdefault(key, value)
        with psycopg.connect(**connection_params(config), row_factory=dict_row, autocommit=True) as connection:
            if args.action == "status":
                result = {"sync_mode": "manual", "runs": connection.execute(
                    """SELECT run_id,task_key,status,started_at,finished_at,error_class,output_summary
                         FROM quant.automation_runs WHERE task_key=%s OR task_key LIKE 'citics_holdings_%%'
                        ORDER BY started_at DESC LIMIT 10""", (TASK_KEY,)).fetchall()}
            elif args.action == "start":
                result = start_manual(connection, args.account_key, args.evidence_root)
            else:
                run = connection.execute("SELECT * FROM quant.automation_runs WHERE run_id=%s", (args.run_id,)).fetchone()
                if not run or run["task_key"] != TASK_KEY:
                    raise ValueError("BROKER_RUN_NOT_FOUND: historical MuMu runs are read-only")
                if args.action == "retry-import":
                    raise ValueError("BROKER_REIMPORT_REQUIRES_NEW_MANUAL_RUN")
                if args.action == "fail":
                    if run["status"] != "running":
                        raise ValueError("BROKER_RUN_ALREADY_TERMINAL")
                    with connection.transaction():
                        fail_run(connection, str(run["run_id"]), ValueError(args.message), error_class=args.error_code)
                    result = {"status": "failed", "error_code": args.error_code,
                              "alert_path": alert_path(args.evidence_root, run["run_id"], args.error_code)}
                elif (run.get("input_summary") or {}).get("account_key") != args.account_key:
                    raise ValueError("BROKER_ACCOUNT_IDENTITY_MISMATCH")
                elif args.action == "confirm-account":
                    if args.envelope is None or args.confirmation_record is None:
                        raise ValueError("BROKER_USER_CONFIRMATION_RECORD_REQUIRED")
                    if args.validate_only:
                        raise ValueError("BROKER_CONFIRMATION_ACTION_REQUIRES_EXPLICIT_WRITE")
                    result = confirm_manual_account(connection, run, args.envelope, args.confirmation_record)
                elif args.action == "prepare-export":
                    if args.session_manifest is None or args.holdings_export is None or args.account_export is None:
                        raise ValueError("BROKER_EXPORT_INPUTS_REQUIRED")
                    if run["status"] != "running":
                        raise ValueError("BROKER_RUN_ALREADY_TERMINAL")
                    result = prepare_export_envelope(
                        run, args.session_manifest, args.holdings_export, args.account_export,
                        args.evidence_root / str(run["run_id"]), trade_path=args.trade_export,
                    )
                elif run["status"] == "completed":
                    result = {**(run.get("output_summary") or {}), "status": "idempotent"}
                elif run["status"] != "running":
                    raise ValueError("BROKER_RUN_ALREADY_TERMINAL")
                else:
                    try:
                        if datetime.now(timezone.utc) - run["started_at"] > MAX_RUN_AGE:
                            raise ValueError("BROKER_MANUAL_RUN_EXPIRED")
                        if args.envelope is None:
                            raise ValueError("BROKER_ENVELOPE_REQUIRED")
                        result = complete_manual(connection, run, args.envelope, args.evidence_root,
                                                 args.base_url, args.adapter_url, validate_only=args.validate_only,
                                                 trade_batch=args.trade_batch)
                    except Exception as error:
                        if not args.validate_only:
                            with connection.transaction():
                                fail_run(connection, str(run["run_id"]), error, error_class=str(error).split(":")[0][:100])
                        raise
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 2 if result.get("status") == "failed" else 0
    except Exception as error:
        code = str(error).split(":", 1)[0][:100]
        folder_id = str(run["run_id"]) if run else "preflight-" + str(uuid.uuid4())
        result = {"status": "failed", "error_code": code,
                  "alert_path": alert_path(args.evidence_root, folder_id, code), "sync_mode": "manual"}
        print(json.dumps(result, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
