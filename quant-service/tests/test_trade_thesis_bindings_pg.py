"""Opt-in native PostgreSQL acceptance for the thesis/discipline binding junction."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from app.instrument_registry import ensure_instruments
from app.trade_thesis.bindings import BindingValidationError, create_binding, load_bound_plan
from app.trade_thesis.repository import capture


pytestmark = pytest.mark.skipif(os.getenv("RUN_TRADE_THESIS_BINDING_PG") != "1",
                                reason="requires an isolated PostgreSQL database")


def test_real_binding_validation_and_hard_risk_projection():
    original_db = os.environ["PGDATABASE"]
    scratch = "thesis_binding_" + uuid4().hex[:12]
    admin = {"host": os.environ["PGHOST"], "port": int(os.environ.get("PGPORT", "5432")),
             "user": os.environ.get("PGADMINUSER", os.environ["PGUSER"]),
             "password": os.environ.get("PGADMINPASSWORD", os.environ["PGPASSWORD"]),
             "dbname": original_db}
    with psycopg.connect(**admin, autocommit=True) as connection:
        connection.execute(f'CREATE DATABASE "{scratch}"')
    env = os.environ.copy()
    env["PGDATABASE"] = scratch
    try:
        admin["dbname"] = scratch
        with psycopg.connect(**admin, autocommit=True) as connection:
            connection.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            connection.execute("CREATE TABLE public.ingestion_jobs(job_id uuid PRIMARY KEY)")
            connection.execute("GRANT CREATE ON DATABASE " + psycopg.sql.Identifier(scratch).as_string(connection)
                               + " TO " + psycopg.sql.Identifier(os.environ["PGUSER"]).as_string(connection))
            connection.execute("GRANT ALL ON TABLE public.ingestion_jobs TO "
                               + psycopg.sql.Identifier(os.environ["PGUSER"]).as_string(connection))
        subprocess.run([sys.executable, "database_bootstrap.py"],
                       cwd=Path(__file__).resolve().parents[1], env=env, check=True,
                       stdout=subprocess.DEVNULL)
        _exercise_binding(scratch)
    finally:
        admin["dbname"] = original_db
        with psycopg.connect(**admin, autocommit=True) as connection:
            connection.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (scratch,))
            connection.execute("DROP DATABASE " + psycopg.sql.Identifier(scratch).as_string(connection))


def _exercise_binding(scratch: str) -> None:
    now = datetime.now(timezone.utc)
    session = now.date()
    while session.weekday() >= 5:
        session -= timedelta(days=1)
    thesis_id = "binding-pg-" + uuid4().hex
    thesis = {"thesis_id": thesis_id, "revision": 1, "symbol": "600000.SH",
              "source_run_id": "binding-pg", "claim": "binding acceptance",
              "available_at": now.isoformat(), "effective_from": now.isoformat(),
              "terminal_deadline": (now + timedelta(days=5)).isoformat(),
              "origin_mode": "prospective", "invariants": [],
              "confirmation_scenarios": [], "invalidation_conditions": []}
    params = {key[2:].lower(): value for key, value in os.environ.items()
              if key in {"PGHOST", "PGPORT", "PGUSER", "PGPASSWORD"}}
    params["dbname"] = scratch
    params["port"] = int(params.get("port", 5432))
    with psycopg.connect(**params, row_factory=dict_row) as connection:
        ensure_instruments(connection, ["600000.SH", "000001.SZ"], source="binding_pg_acceptance")
        connection.execute("""
            INSERT INTO quant.market_trade_calendar(exchange,calendar_date,is_open,available_at)
            VALUES('SSE',%s,true,%s)
        """, (session, now - timedelta(days=1)))
        capture(connection, thesis)
        plan_id = connection.execute("""
            INSERT INTO quant.discipline_plans(
                plan_key,contract_version,account_key,symbol,name,plan_kind,stage,template_key,
                template_version,as_of_at,trading_date,valid_until,position,inputs_hash,
                generator_version,content_hash,status)
            VALUES(%s,'1','acct-a','600000.SH','浦发银行','holding','holding','t','1',%s,%s,%s,
                   %s,'a','binding-test',%s,'active') RETURNING plan_id
        """, ("binding-" + uuid4().hex, now, session, now + timedelta(days=2),
              psycopg.types.json.Json({"snapshot_id": "snap-real", "observed_at": now.isoformat(),
                                       "quantity": 100, "sellable_quantity": 100}), "a" * 64)).fetchone()["plan_id"]
        connection.execute("""
            INSERT INTO quant.discipline_evaluations(
                plan_id,as_of_at,trading_date,basis,line_states,plan_state,inputs_hash,content_hash)
            VALUES(%s,%s,%s,'daily',%s,'exit_signalled','b',%s)
        """, (plan_id, now, session, psycopg.types.json.Json([
            {"kind": "hard_stop", "label": "hard stop", "state": "triggered"}
        ]), "b" * 64))
        bound_at = datetime.now(timezone.utc)
        created = create_binding(
            connection, thesis_id, thesis_revision=1, account_key="acct-a", symbol="600000.SH",
            position_episode_id="episode-real", plan_id=str(plan_id), bound_at=bound_at,
            evidence_refs=["position_snapshot:snap-real"],
        )
        assert created["status"] == "created"
        assert create_binding(
            connection, thesis_id, thesis_revision=1, account_key="acct-a", symbol="600000.SH",
            position_episode_id="episode-real", plan_id=str(plan_id), bound_at=bound_at,
            evidence_refs=["position_snapshot:snap-real"],
        )["status"] == "idempotent"
        loaded = load_bound_plan(connection, thesis_id, account_key="acct-a", as_of=bound_at)
        assert loaded["status"] == "bound" and loaded["holding"]["quantity"] == 100
        assert loaded["risk"]["hard_risk"] is True
        with pytest.raises(BindingValidationError, match="account"):
            create_binding(connection, thesis_id, thesis_revision=1, account_key="acct-b",
                           symbol="600000.SH", position_episode_id="x", plan_id=str(plan_id), bound_at=now)
        with pytest.raises(BindingValidationError, match="symbol"):
            create_binding(connection, thesis_id, thesis_revision=1, account_key="acct-a",
                           symbol="000001.SZ", position_episode_id="x", plan_id=str(plan_id), bound_at=now)
        with pytest.raises(BindingValidationError, match="active"):
            create_binding(
                connection, thesis_id, thesis_revision=1, account_key="acct-a", symbol="600000.SH",
                position_episode_id="expired-episode", plan_id=str(plan_id),
                bound_at=now + timedelta(days=3), evidence_refs=["position_snapshot:snap-real"],
            )
        connection.rollback()
