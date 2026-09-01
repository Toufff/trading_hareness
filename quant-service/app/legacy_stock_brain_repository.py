"""PostgreSQL persistence boundary for the stock-brain migration."""

from __future__ import annotations

from contextlib import contextmanager
import os
from typing import Any, Iterator, Mapping, Sequence
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb


class LegacyStockBrainRepository:
    def __init__(self) -> None:
        self.connection = psycopg.connect(
            host=os.getenv("PGHOST", "127.0.0.1"),
            port=os.getenv("PGPORT", "55432"),
            dbname=os.getenv("PGDATABASE", "trading_hareness"),
            user=os.getenv("PGUSER", "quant_app"),
            password=os.getenv("PGPASSWORD", ""),
            connect_timeout=10,
            autocommit=True,
        )

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Cursor]:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                yield cursor

    def begin_import(
        self,
        *,
        source_snapshot_key: str,
        source_path: str,
        source_sha256: str,
        source_size_bytes: int,
    ) -> tuple[UUID, str]:
        with self.transaction() as cursor:
            cursor.execute(
                """INSERT INTO quant.legacy_import_runs(
                       source_system,source_snapshot_key,source_path,source_sha256,source_size_bytes,status)
                   VALUES('stock-brain',%s,%s,%s,%s,'running')
                   ON CONFLICT(source_system,source_snapshot_key) DO UPDATE SET
                       source_path=excluded.source_path,
                       status=CASE WHEN quant.legacy_import_runs.status='completed'
                                   THEN 'completed' ELSE 'running' END,
                       error_text=NULL
                   RETURNING import_run_id,status""",
                (source_snapshot_key, source_path, source_sha256, source_size_bytes),
            )
            row = cursor.fetchone()
        return row[0], str(row[1])

    def receipt(self, import_run_id: UUID, table: str) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT status,last_rowid,archived_row_count,canonical_row_count,
                          skipped_row_count,source_row_count
                     FROM quant.legacy_import_table_receipts
                    WHERE import_run_id=%s AND source_table=%s""",
                (import_run_id, table),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return {
            "status": row[0], "last_rowid": int(row[1]), "archived": int(row[2]),
            "canonical": int(row[3]), "skipped": int(row[4]), "source": int(row[5]),
        }

    def start_table(
        self,
        *,
        import_run_id: UUID,
        table: str,
        classification: str,
        source_count: int,
    ) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                """INSERT INTO quant.legacy_import_table_receipts(
                       import_run_id,source_table,classification,status,source_row_count,started_at)
                   VALUES(%s,%s,%s,'running',%s,now())
                   ON CONFLICT(import_run_id,source_table) DO UPDATE SET
                       classification=excluded.classification,
                       source_row_count=excluded.source_row_count,
                       status=CASE WHEN quant.legacy_import_table_receipts.status='completed'
                                   THEN 'completed' ELSE 'running' END,
                       error_text=NULL,
                       started_at=coalesce(quant.legacy_import_table_receipts.started_at,now())""",
                (import_run_id, table, classification, source_count),
            )

    def exclude_table(self, import_run_id: UUID, table: str, source_count: int) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                """INSERT INTO quant.legacy_import_table_receipts(
                       import_run_id,source_table,classification,status,source_row_count,
                       skipped_row_count,started_at,completed_at)
                   VALUES(%s,%s,'excluded','excluded',%s,%s,now(),now())
                   ON CONFLICT(import_run_id,source_table) DO UPDATE SET
                       classification='excluded',status='excluded',
                       source_row_count=excluded.source_row_count,
                       skipped_row_count=excluded.skipped_row_count,completed_at=now(),error_text=NULL""",
                (import_run_id, table, source_count, source_count),
            )

    @staticmethod
    def archive_rows(cursor: psycopg.Cursor, import_run_id: UUID, records: Sequence[Mapping[str, Any]]) -> None:
        if not records:
            return
        cursor.execute("""CREATE TEMP TABLE IF NOT EXISTS stock_brain_legacy_stage (
            source_system text,source_table text,source_row_key text,classification text,
            decision_eligible boolean,effective_at timestamptz,available_at timestamptz,
            payload_sha256 text,payload jsonb,first_import_run_id uuid,last_seen_import_run_id uuid
        ) ON COMMIT DELETE ROWS""")
        cursor.execute("TRUNCATE stock_brain_legacy_stage")
        with cursor.copy("""COPY stock_brain_legacy_stage(
            source_system,source_table,source_row_key,classification,decision_eligible,
            effective_at,available_at,payload_sha256,payload,first_import_run_id,last_seen_import_run_id)
            FROM STDIN""") as copy:
            for record in records:
                copy.write_row((
                    "stock-brain", record["table"], record["row_key"], record["classification"], False,
                    record.get("effective_at"), record.get("available_at"), record["payload_sha256"],
                    Jsonb(record["payload"]), import_run_id, import_run_id,
                ))
        cursor.execute("""INSERT INTO quant.legacy_source_records(
               source_system,source_table,source_row_key,classification,decision_eligible,
               effective_at,available_at,payload_sha256,payload,first_import_run_id,last_seen_import_run_id)
            SELECT source_system,source_table,source_row_key,classification,decision_eligible,
                   effective_at,available_at,payload_sha256,payload,first_import_run_id,last_seen_import_run_id
              FROM stock_brain_legacy_stage
            ON CONFLICT(source_system,source_table,source_row_key,payload_sha256) DO UPDATE SET
               last_seen_import_run_id=excluded.last_seen_import_run_id,last_seen_at=now()""")

    @staticmethod
    def upsert_instruments(cursor: psycopg.Cursor, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        deduped = {row["symbol"]: row for row in rows if row.get("symbol")}
        cursor.execute("""CREATE TEMP TABLE IF NOT EXISTS stock_brain_instrument_stage(
            symbol text,exchange text,name text,source text
        ) ON COMMIT DELETE ROWS""")
        cursor.execute("TRUNCATE stock_brain_instrument_stage")
        with cursor.copy("COPY stock_brain_instrument_stage(symbol,exchange,name,source) FROM STDIN") as copy:
            for row in deduped.values():
                copy.write_row((row["symbol"], row["exchange"], row.get("name"), row.get("source", "stock-brain")))
        cursor.execute("""INSERT INTO quant.instruments(symbol,exchange,name,source)
            SELECT symbol,exchange,name,source FROM stock_brain_instrument_stage
            ON CONFLICT(symbol) DO UPDATE SET
                exchange=CASE WHEN quant.instruments.exchange IN ('','UNKNOWN')
                              THEN excluded.exchange ELSE quant.instruments.exchange END,
                name=coalesce(NULLIF(excluded.name,''),quant.instruments.name),updated_at=now()""")

    @staticmethod
    def upsert_money_flows(cursor: psycopg.Cursor, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        cursor.execute("""CREATE TEMP TABLE IF NOT EXISTS stock_brain_flow_stage(
            symbol text,trading_date date,source text,provider text,net_amount numeric,
            available_at timestamptz,raw jsonb
        ) ON COMMIT DELETE ROWS""")
        cursor.execute("TRUNCATE stock_brain_flow_stage")
        with cursor.copy("""COPY stock_brain_flow_stage(
            symbol,trading_date,source,provider,net_amount,available_at,raw) FROM STDIN""") as copy:
            for row in rows:
                copy.write_row((
                    row["symbol"], row["trading_date"], row["source"], row["provider"],
                    row.get("net_amount"), row["available_at"], Jsonb(row["raw"]),
                ))
        cursor.execute("""INSERT INTO quant.stock_money_flow_daily(
               symbol,trading_date,source,provider,net_amount,net_amount_rate,
               buy_elg_amount,buy_lg_amount,buy_md_amount,buy_sm_amount,available_at,raw)
            SELECT symbol,trading_date,source,provider,net_amount,NULL,NULL,NULL,NULL,NULL,available_at,raw
              FROM stock_brain_flow_stage
            ON CONFLICT(symbol,trading_date,source) DO UPDATE SET
               provider=excluded.provider,net_amount=excluded.net_amount,
               available_at=excluded.available_at,raw=excluded.raw""")

    @staticmethod
    def upsert_market_observations(cursor: psycopg.Cursor, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        cursor.executemany(
            """INSERT INTO quant.raw_market_observations(
                   observation_id,provider_key,capability,market,symbol,effective_at,available_at,
                   ingested_at,availability_basis,payload_sha256,normalized,payload)
               VALUES(%s,%s,'legacy_daily_bar',%s,%s,%s,%s,%s,'source_timestamp',%s,%s,%s)
               ON CONFLICT(provider_key,capability,market,symbol,effective_at,payload_sha256)
               DO NOTHING""",
            [(
                row["observation_id"], row["provider"], row["market"], row["symbol"],
                row["effective_at"], row["available_at"], row.get("ingested_at"),
                row["payload_sha256"], Jsonb(row["normalized"]), Jsonb(row["payload"]),
            ) for row in rows],
        )
        settled = [row for row in rows if row.get("settled") and row.get("close") is not None]
        if not settled:
            return
        values = [(
            row["symbol"], row["trading_date"], row.get("open"), row.get("high"), row.get("low"),
            row["close"], row.get("pre_close"), row.get("volume"), row.get("amount"),
            row["provider"], row["available_at"],
        ) for row in settled]
        cursor.executemany(
            """INSERT INTO quant.market_bars_daily(
                   symbol,trading_date,open,high,low,close,pre_close,volume,amount,source,available_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(symbol,trading_date) DO UPDATE SET
                   open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                   pre_close=excluded.pre_close,volume=excluded.volume,amount=excluded.amount,
                   source=excluded.source,available_at=excluded.available_at
               WHERE excluded.available_at >= quant.market_bars_daily.available_at""",
            values,
        )
        cursor.executemany(
            """INSERT INTO quant.canonical_bars_daily(
                   symbol,trading_date,open,high,low,close,pre_close,volume,amount,selected_provider,
                   source_observation_ids,quality_status,available_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'fresh',%s)
               ON CONFLICT(symbol,trading_date) DO UPDATE SET
                   open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                   pre_close=excluded.pre_close,volume=excluded.volume,amount=excluded.amount,
                   selected_provider=excluded.selected_provider,
                   source_observation_ids=excluded.source_observation_ids,
                   quality_status='fresh',available_at=excluded.available_at,canonicalized_at=now()
               WHERE excluded.available_at >= quant.canonical_bars_daily.available_at""",
            [(
                row["symbol"], row["trading_date"], row.get("open"), row.get("high"), row.get("low"),
                row["close"], row.get("pre_close"), row.get("volume"), row.get("amount"),
                row["provider"], [row["observation_id"]], row["available_at"],
            ) for row in settled],
        )

    @staticmethod
    def upsert_journal_entries(cursor: psycopg.Cursor, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        cursor.executemany(
            """INSERT INTO quant.personal_journal_entries(
                   entry_date,entry_type,title,body,positions,actions,plans,watchlist,discipline,
                   source,source_record_key,content_hash,metadata,created_at)
               VALUES(%s,'review',%s,%s,%s,%s,%s,%s,%s,'stock-brain',%s,%s,%s,%s)
               ON CONFLICT(source,source_record_key,content_hash) DO NOTHING""",
            [(
                row["entry_date"], row["title"], row.get("body", ""), Jsonb(row["positions"]),
                Jsonb(row["actions"]), Jsonb(row["plans"]), Jsonb(row["watchlist"]),
                Jsonb(row["discipline"]), row["source_record_key"], row["content_hash"],
                Jsonb(row.get("metadata", {})), row.get("created_at"),
            ) for row in rows],
        )

    @staticmethod
    def upsert_partial_broker_snapshots(cursor: psycopg.Cursor, rows: Sequence[Mapping[str, Any]]) -> None:
        for row in rows:
            cursor.execute(
                """INSERT INTO quant.broker_portfolio_snapshots(
                       account_key,source,source_snapshot_key,observed_at,verification,
                       content_hash,metadata)
                   VALUES('citics-primary','stock_brain.citics_history',%s,%s,'verified_partial',%s,%s)
                   ON CONFLICT(account_key,source,source_snapshot_key) DO UPDATE SET
                       content_hash=excluded.content_hash,metadata=excluded.metadata
                   RETURNING snapshot_id""",
                (
                    row["source_snapshot_key"], row["observed_at"], row["content_hash"],
                    Jsonb(row.get("metadata", {})),
                ),
            )
            snapshot_id = cursor.fetchone()[0]
            positions = row.get("positions", [])
            cursor.executemany(
                """INSERT INTO quant.broker_position_snapshots(
                       snapshot_id,symbol,name,quantity,sellable_quantity,average_cost,
                       position_weight_pct,metadata)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(snapshot_id,symbol) DO UPDATE SET
                       name=excluded.name,quantity=excluded.quantity,
                       sellable_quantity=excluded.sellable_quantity,
                       average_cost=excluded.average_cost,
                       position_weight_pct=excluded.position_weight_pct,metadata=excluded.metadata""",
                [(
                    snapshot_id, position["symbol"], position.get("name") or position["symbol"],
                    position["quantity"], position["sellable_quantity"], position.get("average_cost"),
                    position.get("position_weight_pct"), Jsonb(position.get("metadata", {})),
                ) for position in positions],
            )

    @staticmethod
    def update_receipt(
        cursor: psycopg.Cursor,
        *,
        import_run_id: UUID,
        table: str,
        last_rowid: int,
        archived: int,
        canonical: int,
        skipped: int,
        completed: bool = False,
    ) -> None:
        cursor.execute(
            """UPDATE quant.legacy_import_table_receipts SET
                   last_rowid=%s,archived_row_count=%s,canonical_row_count=%s,
                   skipped_row_count=%s,status=CASE WHEN %s THEN 'completed' ELSE 'running' END,
                   completed_at=CASE WHEN %s THEN now() ELSE NULL END,error_text=NULL
               WHERE import_run_id=%s AND source_table=%s""",
            (last_rowid, archived, canonical, skipped, completed, completed, import_run_id, table),
        )

    def finish_import(self, import_run_id: UUID, summary: Mapping[str, Any]) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                """UPDATE quant.legacy_import_runs
                      SET status='completed',completed_at=now(),summary=%s,error_text=NULL
                    WHERE import_run_id=%s""",
                (Jsonb(dict(summary)), import_run_id),
            )

    def fail_import(self, import_run_id: UUID, error: str) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                """UPDATE quant.legacy_import_runs SET status='failed',completed_at=now(),error_text=%s
                    WHERE import_run_id=%s""",
                (error[:4000], import_run_id),
            )


__all__ = ["LegacyStockBrainRepository"]
