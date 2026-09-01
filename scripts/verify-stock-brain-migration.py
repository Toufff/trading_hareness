#!/usr/bin/env python3
"""Real PostgreSQL acceptance check for the stock-brain migration path."""

from __future__ import annotations

from pathlib import Path
import os
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quant-service"))

from app.legacy_stock_brain_archive import LegacyStockBrainArchiveImporter, sha256_file  # noqa: E402
from app.legacy_stock_brain_repository import LegacyStockBrainRepository  # noqa: E402


def build_fixture(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE documents(id integer primary key,title text,verification text,created_at text);
        INSERT INTO documents VALUES(1,'原始公告','verified','2026-08-31T16:00:00+08:00');
        CREATE TABLE security_order_flow_daily(
          security_code text,security_name text,trade_date text,source_key text,convention text,
          main_net real,super_large_net real,large_net real,medium_net real,small_net real,
          close_price real,change_pct real,available_at text,received_at text,raw_json text);
        INSERT INTO security_order_flow_daily VALUES(
          '600664','哈药股份','2026-08-31','market:test','vendor_main_net',1000000,
          NULL,NULL,NULL,NULL,12.3,1.2,'2026-08-31T15:30:00+08:00','2026-08-31T15:31:00+08:00','{}');
        CREATE TABLE market_observations(
          id integer primary key,trade_date text,symbol text,name text,observed_at text,received_at text,
          source text,session_state text,price real,prev_close real,open real,high real,low real,
          volume real,amount real,quality text,provenance_json text,payload_hash text);
        INSERT INTO market_observations VALUES(
          1,'2026-08-31','sh000001','上证指数','2026-08-31T15:10:00+08:00',
          '2026-08-31T15:10:01+08:00','fixture','closed',4000,3980,3990,4010,3970,
          100,200,'settled','{}','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
        CREATE TABLE personal_reviews(
          id integer primary key,date text,positions text,action text,reflection text,plan text,
          watchlist text,discipline text,created_at text);
        INSERT INTO personal_reviews VALUES(
          1,'2026-08-31','[]','["复盘"]','保留事实','[]','[]','{"止损":true}',
          '2026-08-31T20:00:00+08:00');
        CREATE TABLE position_snapshots(
          id integer primary key,portfolio_id integer,entity_id integer,security_code text,as_of text,
          weight real,quantity real,average_cost real,source text,created_at text,
          available_quantity real,security_name text,benchmark_code text,plan_json text,triggers_json text);
        INSERT INTO position_snapshots VALUES(
          1,1,1,'600664','2026-08-31T15:18:00+08:00',0.5,1000,11.8,'CITIC readonly',
          '2026-08-31T15:19:00+08:00',1000,'哈药股份','000001.SH','{"bad":"discard"}','{}');
        CREATE TABLE decision_session_runs(id integer primary key,status text);
        INSERT INTO decision_session_runs VALUES(1,'failed');
    """)
    connection.commit()
    connection.close()


def main() -> int:
    if not os.getenv("PGDATABASE", "").endswith("_acceptance"):
        raise SystemExit("refusing to run against a non-acceptance database")
    with TemporaryDirectory() as directory:
        fixture = Path(directory) / "brain.db"
        build_fixture(fixture)
        repository = LegacyStockBrainRepository()
        try:
            result = LegacyStockBrainArchiveImporter(repository, batch_size=100).run(fixture, sha256_file(fixture))
            assert result["status"] == "completed", result
            with repository.connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM quant.legacy_source_records")
                assert cursor.fetchone()[0] == 5
                cursor.execute("SELECT net_amount FROM quant.stock_money_flow_daily WHERE symbol='600664.SH'")
                assert float(cursor.fetchone()[0]) == 1_000_000
                cursor.execute("SELECT close FROM quant.canonical_bars_daily WHERE symbol='000001.SH'")
                assert float(cursor.fetchone()[0]) == 4000
                cursor.execute("SELECT count(*) FROM quant.personal_journal_entries")
                assert cursor.fetchone()[0] == 1
                cursor.execute("SELECT count(*) FROM quant.broker_portfolio_snapshots")
                assert cursor.fetchone()[0] == 1
                cursor.execute("SELECT count(*) FROM quant.legacy_source_records WHERE source_table='decision_session_runs'")
                assert cursor.fetchone()[0] == 0
            rerun = LegacyStockBrainArchiveImporter(repository, batch_size=100).run(fixture, sha256_file(fixture))
            assert rerun["status"] == "already_completed", rerun
        finally:
            repository.close()
    print("stock-brain migration acceptance: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
