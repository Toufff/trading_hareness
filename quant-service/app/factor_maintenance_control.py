"""One session owns the lock AND all factor transactions: no unfenced reconnect.

Trust-based operational boundary, not a restriction on the peer's SQL grants.
Only the three factor projections are journalled; scheduler receipts are not
rewound. A rollback restores data, not the correctness of the former data.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import os
import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .db_dsn import connection_params

LOCK_KEY = 72814409749381
TABLE_COLUMNS = {
    'canonical_bars_daily': ('adj_factor',),
    'market_bars_daily': ('adj_factor',),
    'daily_adjustment_factors': ('symbol', 'trading_date', 'provider', 'adj_factor', 'available_at', 'raw'),
}


def columns_for(table: str) -> tuple[str, ...]:
    if table not in TABLE_COLUMNS:
        raise ValueError('not a factor-maintenance table')
    return TABLE_COLUMNS[table]


def connect():
    return psycopg.connect(**connection_params(), autocommit=True, row_factory=dict_row,
        connect_timeout=10, application_name='factor-maintenance',
        options='-c statement_timeout=600000 -c lock_timeout=10000 -c idle_in_transaction_session_timeout=300000')


class SessionDatabase:
    def __init__(self, connection, run_id):
        self.connection = connection
        self.run_id = str(run_id)

    @contextmanager
    def transaction(self, *, statement_timeout_ms=None):
        # Never creates another connection. A lost lock connection is fatal.
        with self.connection.transaction():
            self.connection.execute("SELECT set_config('quant.factor_run_id',%s,true)", (self.run_id,))
            if statement_timeout_ms is not None:
                self.connection.execute("SELECT set_config('statement_timeout',%s,true)", (str(int(statement_timeout_ms)),))
            yield self.connection
            # Receipt and journal count commit with each date, including runs
            # interrupted before finish(). A missing/archived journal must never
            # make rollback silently restore only a subset of the run.
            self.connection.execute("""UPDATE quant.factor_maintenance_runs SET change_count=(
                SELECT count(*) FROM quant.factor_maintenance_changes WHERE run_id=%s)
                WHERE run_id=%s""", (self.run_id,self.run_id))


def start(connection, operation, parameters):
    if not connection.execute('SELECT pg_try_advisory_lock(%s) AS acquired', (LOCK_KEY,)).fetchone()['acquired']:
        return None
    # No stale process can hold this lock: it dies with its DB connection.
    # Acquiring it proves previous 'running' runs are abandoned, not active.
    with connection.transaction():
        triggers = connection.execute("""SELECT c.relname FROM pg_trigger t
            JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='quant' AND t.tgname='factor_maintenance_capture'
              AND t.tgenabled IN ('O','A') AND NOT t.tgisinternal""").fetchall()
        if not set(TABLE_COLUMNS).issubset({row['relname'] for row in triggers}):
            raise RuntimeError('factor maintenance audit triggers missing or disabled; migrate before writing')
        connection.execute("""UPDATE quant.factor_maintenance_runs SET status='interrupted',
            finished_at=clock_timestamp() WHERE status='running'""")
        run_id = uuid4()
        connection.execute("""INSERT INTO quant.factor_maintenance_runs
            (run_id,operation,actor,source_version,parameters) VALUES(%s,%s,%s,%s,%s)""",
            (run_id, operation, os.getenv('QUANT_FACTOR_ACTOR') or 'owner-scheduled-or-cli',
             os.getenv('APP_GIT_SHA') or hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), Jsonb(parameters)))
    return run_id


def finish(connection, run_id, result):
    connection.execute("""UPDATE quant.factor_maintenance_runs SET status=%s,
        finished_at=clock_timestamp(),result=%s WHERE run_id=%s""",
        (result.get('status', 'failed'), Jsonb(result), run_id))


async def managed_run(dependencies, operation, action, parameters):
    connection = await asyncio.to_thread(connect)
    try:
        run_id = await asyncio.to_thread(start, connection, operation,
            {k:str(v) if v is not None else None for k,v in parameters.items()})
        if run_id is None:
            return {'status':'busy', 'retryable':True, 'reason':'another factor maintenance run owns the database lock'}
        scoped = replace(dependencies, database=SessionDatabase(connection,run_id), control=None)
        try:
            result = await action(scoped, **parameters)
            result['maintenance_run_id'] = str(run_id)
            await asyncio.to_thread(finish, connection, run_id, result)
            return result
        except BaseException as exc:
            # Error text may contain a DSN/provider payload. Persist class only.
            try:
                await asyncio.to_thread(finish, connection, run_id,
                    {'status':'failed','error_class':type(exc).__name__})
            except Exception:
                pass  # A dead connection leaves a truthful interrupted run on next acquisition.
            raise
    finally:
        # Close rather than returning a session lock to a connection pool.
        await asyncio.to_thread(connection.close)


def history(connection, run_id=None, limit=20):
    if run_id:
        UUID(str(run_id))
        rows = connection.execute('SELECT * FROM quant.factor_maintenance_runs WHERE run_id=%s', (run_id,)).fetchall()
    else:
        rows = connection.execute('SELECT * FROM quant.factor_maintenance_runs ORDER BY started_at DESC LIMIT %s',
                                  (max(1,min(100,limit)),)).fetchall()
    return {'runs':rows}


def _key_predicate(key):
    if set(key) not in ({'symbol','trading_date'}, {'symbol','trading_date','provider'}):
        raise ValueError('invalid journal key')
    keys = sorted(key)
    return sql.SQL(' AND ').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in keys), [key[k] for k in keys]


def _current(connection, table, key, *, lock=True):
    columns = columns_for(table)
    predicate, values = _key_predicate(key)
    query = sql.SQL('SELECT to_jsonb(v)::text AS image FROM (SELECT {} FROM quant.{} WHERE {} {}) v').format(
        sql.SQL(',').join(map(sql.Identifier, columns)), sql.Identifier(table), predicate,
        sql.SQL('FOR UPDATE' if lock else ''))
    row = connection.execute(query, values).fetchone()
    return row['image'] if row else None


def assert_recorded_image(connection, change):
    if _current(connection,change['table_name'],change['row_key']) != change['after_image']:
        raise ValueError('rollback conflict: data changed after recorded write')


def _restore(connection, change):
    table, key, before = change['table_name'], change['row_key'], change['before_image']
    columns = columns_for(table)
    assert_recorded_image(connection, change)
    predicate, values = _key_predicate(key)
    if table != 'daily_adjustment_factors':
        if before is None:
            raise ValueError('rollback cannot delete or recreate price bars')
        # Restoring a recorded factor, not promoting new evidence. CAS was checked
        # under row lock by rollback_changes; original prices are never restored.
        restore_sql = {
            'canonical_bars_daily': "UPDATE quant.canonical_bars_daily SET adj_factor=(%s::jsonb->>'adj_factor')::numeric WHERE {}",
            'market_bars_daily': "UPDATE quant.market_bars_daily SET adj_factor=(%s::jsonb->>'adj_factor')::numeric WHERE {}",
        }
        connection.execute(sql.SQL(restore_sql[table]).format(predicate), [before,*values])
    elif before is None:
        connection.execute(sql.SQL('DELETE FROM quant.{} WHERE {}').format(sql.Identifier(table),predicate), values)
    else:
        names = sql.SQL(',').join(map(sql.Identifier, columns))
        connection.execute(sql.SQL('''INSERT INTO quant.{} ({})
            SELECT {} FROM jsonb_populate_record(NULL::quant.daily_adjustment_factors,%s::jsonb)
            ON CONFLICT(symbol,trading_date,provider) DO UPDATE SET
              adj_factor=EXCLUDED.adj_factor,available_at=EXCLUDED.available_at,raw=EXCLUDED.raw''').format(
                sql.Identifier(table),names,names), (before,))


def rollback_changes(connection, source_run_id, *, apply=False):
    """Whole-run CAS rollback, one transaction; dry-run writes nothing.

    The latest-run requirement avoids ABA and later repairs built on this run's
    anchors. Failed runs are eligible: committed dates still have exact journals.
    """
    source_run_id = str(UUID(str(source_run_id)))
    with connection.transaction():
        run = connection.execute('SELECT * FROM quant.factor_maintenance_runs WHERE run_id=%s', (source_run_id,)).fetchone()
        if not run:
            raise ValueError('maintenance run not found')
        if run['status'] in ('running','rolled_back') or run['operation']=='rollback':
            raise ValueError('run is not eligible for rollback')
        later = connection.execute('''SELECT 1 FROM quant.factor_maintenance_changes c
            JOIN quant.factor_maintenance_runs r USING(run_id)
            WHERE c.change_no > (SELECT max(change_no) FROM quant.factor_maintenance_changes WHERE run_id=%s)
              AND c.run_id<>%s AND r.operation<>'rollback' AND r.status<>'rolled_back' LIMIT 1''',
              (source_run_id,source_run_id)).fetchone()
        if later:
            raise ValueError('later factor changes exist; roll back newest change first')
        changes = connection.execute('''SELECT table_name,row_key,before_image::text,after_image::text
            FROM quant.factor_maintenance_changes WHERE run_id=%s ORDER BY change_no DESC''', (source_run_id,)).fetchall()
        if len(changes) != run['change_count']:
            raise ValueError('rollback journal incomplete or archived; manual audit recovery required')
        if run['started_at'] < connection.execute("SELECT now()-interval '365 days' AS cutoff").fetchone()['cutoff']:
            raise ValueError('automatic rollback window expired; use archived audit with manual review')
        simulated = {}
        for item in changes:
            key = (item['table_name'],tuple(sorted(item['row_key'].items())))
            current = simulated.get(key) if key in simulated else _current(connection, item['table_name'], item['row_key'], lock=apply)
            if current != item['after_image']:
                raise ValueError('rollback conflict: data changed after recorded write')
            if item['table_name'] != 'daily_adjustment_factors' and item['before_image'] is None:
                raise ValueError('rollback cannot delete price bars')
            simulated[key] = item['before_image']
        if apply:
            for item in changes:
                _restore(connection,item)
            connection.execute("UPDATE quant.factor_maintenance_runs SET status='rolled_back' WHERE run_id=%s", (source_run_id,))
        return {'status':'completed' if apply else 'planned','source_run_id':source_run_id,
                'change_count':len(changes),'apply':apply,'warning':'restores prior data, not a guarantee that prior factors were correct'}
