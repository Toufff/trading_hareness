"""One-shot isolated PostgreSQL acceptance; prints no connection settings."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "quant-service"
ENV_FILE = Path(r"G:\StockPlatform\config\runtime.env")


def load_env():
    for raw in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def params(dbname=None):
    return {"host": os.environ["PGHOST"], "port": os.environ.get("PGPORT", "5432"),
            "user": os.environ["PGUSER"], "password": os.environ["PGPASSWORD"],
            "dbname": dbname or os.environ["PGDATABASE"]}


def admin_params(dbname=None):
    value = params(dbname)
    value["user"] = os.environ.get("PGADMINUSER", value["user"])
    value["password"] = os.environ.get("PGADMINPASSWORD", value["password"])
    return value


def main():
    load_env()
    original_db = os.environ["PGDATABASE"]
    scratch = "thesis_accept_" + uuid4().hex[:12]
    env = os.environ.copy()
    env["PGDATABASE"] = scratch
    with psycopg.connect(**admin_params(original_db), autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{scratch}"')
    try:
        with psycopg.connect(**admin_params(scratch), autocommit=True) as conn:
            conn.execute("GRANT CREATE ON DATABASE " + psycopg.sql.Identifier(scratch).as_string(conn)
                         + " TO " + psycopg.sql.Identifier(os.environ["PGUSER"]).as_string(conn))
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            conn.execute("CREATE TABLE public.ingestion_jobs(job_id uuid PRIMARY KEY)")
            conn.execute("GRANT ALL ON TABLE public.ingestion_jobs TO "
                         + psycopg.sql.Identifier(os.environ["PGUSER"]).as_string(conn))
        subprocess.run([sys.executable, "database_bootstrap.py"], cwd=SERVICE,
                       env=env, check=True, stdout=subprocess.DEVNULL)
        sys.path.insert(0, str(SERVICE))
        from app.trade_thesis.repository import ThesisConflict, propose_change, review_change
        from app.trade_thesis.service import capture_evaluate
        from app.database import Database
        from app.instrument_registry import ensure_instruments

        old_db = original_db
        os.environ["PGDATABASE"] = scratch
        database = Database()
        thesis_id = "accept-" + uuid4().hex
        now = datetime.now(timezone.utc)
        thesis = {"thesis_id": thesis_id, "revision": 1, "symbol": "600000.SH",
                  "source_run_id": "origin-run", "claim": "isolated acceptance",
                  "available_at": now.isoformat(), "effective_from": now.isoformat(),
                  "terminal_deadline": (now + timedelta(days=5)).isoformat(),
                  "origin_mode": "prospective", "invariants": [],
                  "confirmation_scenarios": [], "invalidation_conditions": []}
        with psycopg.connect(**params(scratch), row_factory=dict_row) as conn:
            ensure_instruments(conn, ["600000.SH"], source="isolated_acceptance")
            conn.commit()
        first = capture_evaluate(database, thesis, [], now.isoformat(), "current-run")
        second = capture_evaluate(database, thesis, [], now.isoformat(), "current-run")
        assert first["status"] == "created" and second["status"] == "idempotent"
        assert first["evaluation"]["source_run_id"] == "current-run"
        with psycopg.connect(**params(scratch), row_factory=dict_row) as conn:
            frozen = conn.execute("SELECT payload FROM quant.trade_thesis_revisions WHERE thesis_id=%s",
                                  (thesis_id,)).fetchone()["payload"]
            assert frozen["source_run_id"] == "origin-run" and frozen["terminal_deadline"] == thesis["terminal_deadline"]
            proposal = propose_change(conn, thesis_id, expected_revision=1,
                                      changes={"claim": "review me"}, actor="author-a")
            conn.commit()
            rejected = review_change(conn, thesis_id, revision=2, proposal_hash=proposal["content_hash"],
                                     verdict="reject", reviewer="reviewer-b", reason="counterexample",
                                     evidence_hash="b" * 64)
            conn.commit()
            assert rejected["status"] == "created"

        def compete(actor):
            try:
                with psycopg.connect(**params(scratch), row_factory=dict_row) as conn:
                    value = propose_change(conn, thesis_id, expected_revision=1,
                                           changes={"claim": actor}, actor=actor)
                    conn.commit()
                    return value["status"]
            except ThesisConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(compete, ["author-c", "author-d"]))
        assert outcomes == ["conflict", "created"], outcomes

        storage = ROOT / "scripts" / "database-storage-tiers.py"
        base = [sys.executable, str(storage), "--env-file", str(ROOT / "artifacts" / "no-runtime-env"),
                "--table", "quant.trade_thesis_evaluations", "--tablespace", "stock_cold"]
        subprocess.run(base[:2] + ["install"] + base[2:] + ["--skip-role-settings"], env=env,
                       check=True, stdout=subprocess.DEVNULL)
        with psycopg.connect(**params(scratch), row_factory=dict_row) as conn:
            row = dict(first["evaluation"])
            old_id = "old-" + uuid4().hex
            conn.execute("""INSERT INTO quant.trade_thesis_evaluations(
                evaluation_id,thesis_id,thesis_revision,source_run_id,cutoff_at,namespace,
                thesis_state,evidence_status,entry_state,input_hash,result,content_hash,created_at)
                VALUES(%s,%s,1,%s,%s,'shadow','pending','partial','waiting',%s,%s,%s,%s)""",
                (old_id, thesis_id, "old-run", now - timedelta(days=400), "c" * 64,
                 json.dumps(row), "d" * 64, now - timedelta(days=400)))
            conn.commit()
        moved = subprocess.run(base[:2] + ["apply"] + base[2:] + ["--hot-days", "365", "--max-batches", "2"],
                               env=env, check=False, capture_output=True, text=True)
        receipt = json.loads(moved.stdout.strip().splitlines()[-1])
        assert receipt["tables"][0]["deleted_rows"] >= 1, receipt["tables"][0]
        with psycopg.connect(**admin_params(scratch), row_factory=dict_row) as conn:
            assert conn.execute("SELECT count(*) n FROM quant.trade_thesis_evaluations_cold WHERE evaluation_id=%s", (old_id,)).fetchone()["n"] == 1
            assert conn.execute("SELECT count(*) n FROM quant.trade_thesis_evaluations_all WHERE evaluation_id=%s", (old_id,)).fetchone()["n"] == 1
            conn.execute("INSERT INTO quant.trade_thesis_evaluations SELECT * FROM quant.trade_thesis_evaluations_cold WHERE evaluation_id=%s", (old_id,))
            conn.execute("DELETE FROM quant.trade_thesis_evaluations_cold WHERE evaluation_id=%s", (old_id,))
            conn.commit()
            valid = conn.execute("""SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
                                     WHERE c.relname='trade_thesis_evaluations_tier_cutoff_idx'""").fetchone()
            assert valid and valid["indisvalid"]
        os.environ["PGDATABASE"] = old_db or ""
        print(json.dumps({"status": "ok", "migration": "head", "idempotent": True,
                          "concurrency": outcomes, "tier_move_restore": True,
                          "capture_immutable": True}, sort_keys=True))
    finally:
        with psycopg.connect(**admin_params(original_db), autocommit=True) as admin:
            # Never remove a concurrent agent's scratch database.
            names = [row[0] for row in admin.execute(
                "SELECT datname FROM pg_database WHERE datname=%s", (scratch,)
            ).fetchall()]
            for name in names:
                admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (name,))
                admin.execute("DROP DATABASE " + psycopg.sql.Identifier(name).as_string(admin))


if __name__ == "__main__":
    main()
