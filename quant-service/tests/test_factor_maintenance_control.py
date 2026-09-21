"""The collaborator uses the same guarded, auditable maintenance entry points."""
import importlib.util
from pathlib import Path
import unittest
import os
import asyncio
from datetime import date
from decimal import Decimal
from uuid import uuid4
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class ControlContractTests(unittest.TestCase):
    def test_peer_concurrency_matches_gateway_without_changing_owner_default(self):
        from app.adjustment_factor_maintenance import longhu_fetch_workers
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(longhu_fetch_workers(),16)
        with patch.dict(os.environ, {'QUANT_FACTOR_FETCH_WORKERS':'4'}):
            self.assertEqual(longhu_fetch_workers(),4)
        with patch.dict(os.environ, {'QUANT_FACTOR_FETCH_WORKERS':'0'}):
            with self.assertRaises(ValueError):
                longhu_fetch_workers()

    def test_rollback_table_allowlist(self):
        from app.factor_maintenance_control import columns_for
        self.assertEqual(columns_for('canonical_bars_daily'), ('adj_factor',))
        self.assertEqual(columns_for('market_bars_daily'), ('adj_factor',))
        self.assertIn('raw', columns_for('daily_adjustment_factors'))
        with self.assertRaises(ValueError):
            columns_for('instruments')

    def test_cli_supports_inherited_environment_and_rollback(self):
        spec = importlib.util.spec_from_file_location('factor_cli', ROOT/'scripts/adjustment-factor-maintenance.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        args = module.parse_args(['repair', '--env-file', '-', '--actor', 'peer-maintainer'])
        self.assertEqual(args.env_file, '-')
        self.assertFalse(args.apply)
        args = module.parse_args(['rollback', '--run-id', '00000000-0000-0000-0000-000000000001'])
        self.assertFalse(args.apply)

    def test_contract_grants_factor_maintenance_without_price_write_promise(self):
        from app.peer_contract import SUPPORTED_OBJECTS, FACTOR_MAINTENANCE
        rows = {item.name:item for item in SUPPORTED_OBJECTS}
        self.assertEqual(rows['daily_adjustment_factors'].access, 'read-write')
        self.assertEqual(FACTOR_MAINTENANCE['bar_write_columns'], ['adj_factor'])
        self.assertFalse(FACTOR_MAINTENANCE['database_column_isolation'])

    def test_owner_composition_wires_control(self):
        import inspect
        from app.main import adjustment_factor_maintenance_dependencies
        self.assertIn('control=managed_run', inspect.getsource(adjustment_factor_maintenance_dependencies))

    def test_peer_launcher_pins_image_isolates_batch_and_never_puts_secrets_in_argv(self):
        spec = importlib.util.spec_from_file_location('factor_peer', ROOT/'scripts/shared-peer/factor-maintenance-peer.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        container = {'Image':'sha256:fixture','State':{'Running':True},
                     'NetworkSettings':{'Networks':{'trading-hareness-peer_default':{}}},
                     'Config':{'Env':['PGUSER=stock_peer','PGPASSWORD=fixture-secret',
                         'PGDATABASE=fixture','PGHOST=db-tunnel','PGPORT=5432',
                         'QUANT_SHARED_READ_API_BASE_URL=http://db-tunnel:5681',
                         'QUANT_SHARED_READ_API_KEY=fixture-key','LONGHU_API_KEY=do-not-copy']}}
        command, env = module.launch_spec(container, Path('/bundle/fixture-sha'), ['repair'])
        self.assertNotIn('fixture-secret', ' '.join(command))
        self.assertNotIn('fixture-key', ' '.join(command))
        self.assertNotIn('LONGHU_API_KEY',command)
        self.assertIn('sha256:fixture',command)
        self.assertIn('--read-only',command)
        self.assertEqual((env['PGHOST'],env['PGPORT']),('db-batch-tunnel','5433'))
        self.assertEqual(env['QUANT_FACTOR_FETCH_WORKERS'],'4')
        self.assertEqual(command[-2:],['--env-file','-'])
        self.assertNotIn('--cpus',command)
        cpu_command, _ = module.launch_spec(container, Path('/bundle/fixture-sha'), ['probe'], cpu_quota_supported=True)
        self.assertIn('--cpus',cpu_command)


@unittest.skipUnless(os.getenv('PGDATABASE','').startswith('stock_audit_test_'), 'requires disposable stock_audit_test_ database')
class ControlDatabaseTests(unittest.TestCase):
    def setUp(self):
        from app.factor_maintenance_control import connect
        self.c = connect()
        self.c.execute("INSERT INTO quant.instruments(symbol,exchange,source) SELECT '000001.SZ','SZ','fixture' ORDER BY 1 ON CONFLICT DO NOTHING")
        self.c.execute("""INSERT INTO quant.canonical_bars_daily
            (symbol,trading_date,close,adj_factor,selected_provider,quality_status,available_at)
            VALUES('000001.SZ','2026-09-18',10,2,'fixture','fresh',now()) ON CONFLICT(symbol,trading_date) DO UPDATE SET adj_factor=2""")
        self.c.execute("""INSERT INTO quant.market_bars_daily(symbol,trading_date,close,adj_factor,source)
            VALUES('000001.SZ','2026-09-18',10,2,'fixture') ON CONFLICT(symbol,trading_date) DO UPDATE SET adj_factor=2""")
        self.c.execute('DELETE FROM quant.factor_maintenance_changes')
        self.c.execute('DELETE FROM quant.factor_maintenance_runs')
        self.c.execute("DELETE FROM quant.daily_adjustment_factors WHERE symbol='000001.SZ'")

    def tearDown(self):
        self.c.close()

    def write_run(self, value='3.1234567890123456789', *, fail=False, twice=False):
        from app.factor_maintenance_control import start, SessionDatabase, finish, LOCK_KEY
        run_id = start(self.c,'repair',{})
        db = SessionDatabase(self.c,run_id)
        try:
            with db.transaction() as conn:
                conn.execute("UPDATE quant.canonical_bars_daily SET adj_factor=%s WHERE symbol='000001.SZ'", (Decimal(value),))
                conn.execute("UPDATE quant.market_bars_daily SET adj_factor=%s WHERE symbol='000001.SZ'", (Decimal(value),))
                if twice:
                    conn.execute("UPDATE quant.canonical_bars_daily SET adj_factor=4 WHERE symbol='000001.SZ'")
                if fail:
                    raise ValueError('fixture transaction failure')
        except ValueError:
            finish(self.c,run_id,{'status':'failed'})
        else:
            finish(self.c,run_id,{'status':'completed'})
        self.c.execute('SELECT pg_advisory_unlock(%s)', (LOCK_KEY,))
        return run_id

    def test_journal_exact_numeric_and_rollback(self):
        from app.factor_maintenance_control import rollback_changes
        run = self.write_run()
        image = self.c.execute('SELECT after_image::text AS v FROM quant.factor_maintenance_changes LIMIT 1').fetchone()['v']
        self.assertIn('3.1234567890123456789',image)
        self.assertEqual(rollback_changes(self.c,run)['change_count'],2)
        rollback_changes(self.c,run,apply=True)
        bar = self.c.execute("SELECT close,adj_factor FROM quant.canonical_bars_daily WHERE symbol='000001.SZ'").fetchone()
        self.assertEqual(bar,{'close':Decimal(10),'adj_factor':Decimal(2)})

    def test_same_row_multiple_writes_reverse_order(self):
        from app.factor_maintenance_control import rollback_changes
        run = self.write_run(twice=True)
        self.assertEqual(rollback_changes(self.c,run,apply=True)['change_count'],3)

    def test_transaction_failure_keeps_no_journal_or_partial_write(self):
        run = self.write_run(fail=True)
        self.assertEqual(self.c.execute('SELECT count(*) AS n FROM quant.factor_maintenance_changes').fetchone()['n'],0)
        self.assertEqual(self.c.execute("SELECT adj_factor FROM quant.canonical_bars_daily WHERE symbol='000001.SZ'").fetchone()['adj_factor'],2)

    def test_conflict_rejects_whole_rollback(self):
        from app.factor_maintenance_control import rollback_changes
        run = self.write_run('3')
        self.c.execute("UPDATE quant.canonical_bars_daily SET adj_factor=9 WHERE symbol='000001.SZ'")
        with self.assertRaisesRegex(ValueError,'conflict'):
            rollback_changes(self.c,run,apply=True)
        self.assertEqual(self.c.execute("SELECT adj_factor FROM quant.market_bars_daily WHERE symbol='000001.SZ'").fetchone()['adj_factor'],3)

    def test_newer_run_must_be_reverted_first(self):
        from app.factor_maintenance_control import rollback_changes
        older = self.write_run('3')
        self.write_run('4')
        with self.assertRaisesRegex(ValueError,'later factor changes'):
            rollback_changes(self.c,older,apply=True)

    def test_lock_is_cross_connection_and_released_on_disconnect(self):
        from app.factor_maintenance_control import connect, start
        self.assertIsNotNone(start(self.c,'repair',{}))
        with connect() as second:
            self.assertIsNone(start(second,'sync',{}))
            self.c.close()
            self.assertIsNotNone(start(second,'sync',{}))

    def test_lost_session_cannot_reconnect_and_write(self):
        from app.factor_maintenance_control import SessionDatabase,start
        import psycopg
        db = SessionDatabase(self.c,start(self.c,'repair',{}))
        self.c.close()
        with self.assertRaises(psycopg.OperationalError):
            with db.transaction():
                pass

    def test_preview_is_server_read_only(self):
        from app.factor_maintenance_control import rollback_changes
        run = self.write_run('3')
        with self.c.transaction():
            self.c.execute('SET TRANSACTION READ ONLY')
            self.assertEqual(rollback_changes(self.c,run)['status'],'planned')
        self.assertEqual(self.c.execute('SELECT count(*) AS n FROM quant.factor_maintenance_changes').fetchone()['n'],2)

    def test_noop_updates_do_not_grow_audit(self):
        self.write_run('2')
        self.assertEqual(self.c.execute('SELECT count(*) AS n FROM quant.factor_maintenance_changes').fetchone()['n'],0)

    def test_factor_insert_and_update_roundtrip_preserves_json_numbers(self):
        from app.factor_maintenance_control import start,SessionDatabase,finish,rollback_changes,LOCK_KEY
        self.c.execute("""INSERT INTO quant.daily_adjustment_factors(symbol,trading_date,provider,adj_factor,available_at,raw)
            VALUES('000001.SZ','2026-09-18','longhu_qfq_derived',2,now(),'{"precision":1.1234567890123456789}')""")
        before = self.c.execute('SELECT to_jsonb(f)::text AS v FROM quant.daily_adjustment_factors f').fetchone()['v']
        run = start(self.c,'repair',{})
        with SessionDatabase(self.c,run).transaction() as conn:
            conn.execute("UPDATE quant.daily_adjustment_factors SET adj_factor=4,raw='{}' WHERE symbol='000001.SZ'")
            conn.execute("""INSERT INTO quant.daily_adjustment_factors(symbol,trading_date,provider,adj_factor,available_at,raw)
                VALUES('000001.SZ','2026-09-18','fixture',5,now(),'{}')""")
        finish(self.c,run,{'status':'completed'})
        self.c.execute('SELECT pg_advisory_unlock(%s)',(LOCK_KEY,))
        rollback_changes(self.c,run,apply=True)
        after = self.c.execute('SELECT to_jsonb(f)::text AS v FROM quant.daily_adjustment_factors f').fetchone()['v']
        self.assertEqual(before,after)

    def test_managed_run_and_both_entrypoints_really_use_the_guard(self):
        from app.factor_maintenance_control import managed_run
        from app.adjustment_factor_maintenance import AdjustmentFactorMaintenanceDependencies, sync, repair
        async def local(action,*args,**kwargs):
            kwargs.pop('timeout_seconds',None)
            return action(*args,**kwargs)
        deps = AdjustmentFactorMaintenanceDependencies(None,local,None,local,str,control=managed_run)
        async def action(scoped,**kwargs):
            with scoped.database.transaction() as conn:
                conn.execute("UPDATE quant.canonical_bars_daily SET adj_factor=7 WHERE symbol='000001.SZ'")
            return {'status':'completed'}
        report = asyncio.run(managed_run(deps,'repair',action,{}))
        self.assertIn('maintenance_run_id',report)
        # Holding another connection makes both deployed entry shapes busy.
        from app.factor_maintenance_control import LOCK_KEY
        self.c.execute('SELECT pg_advisory_lock(%s)',(LOCK_KEY,))
        self.assertEqual(asyncio.run(sync(deps))['status'],'busy')
        self.assertEqual(asyncio.run(repair(deps,apply=True))['status'],'busy')

    def test_missing_trigger_fails_before_creating_a_run(self):
        from app.factor_maintenance_control import start
        with self.c.transaction():
            self.c.execute('ALTER TABLE quant.canonical_bars_daily DISABLE TRIGGER factor_maintenance_capture')
            with self.assertRaisesRegex(RuntimeError,'triggers missing'):
                start(self.c,'repair',{})
            self.assertEqual(self.c.execute('SELECT count(*) AS n FROM quant.factor_maintenance_runs').fetchone()['n'],0)
            self.c.execute('ALTER TABLE quant.canonical_bars_daily ENABLE TRIGGER factor_maintenance_capture')

    def test_newest_first_rollback_chain_remains_possible(self):
        from app.factor_maintenance_control import rollback_changes
        older = self.write_run('3')
        newer = self.write_run('4')
        rollback_changes(self.c,newer,apply=True)
        rollback_changes(self.c,older,apply=True)
        self.assertEqual(self.c.execute("SELECT adj_factor FROM quant.canonical_bars_daily WHERE symbol='000001.SZ'").fetchone()['adj_factor'],2)

    def test_incomplete_or_archived_journal_refuses_partial_rollback(self):
        from app.factor_maintenance_control import rollback_changes
        run = self.write_run('3')
        self.c.execute("DELETE FROM quant.factor_maintenance_changes WHERE table_name='market_bars_daily'")
        with self.assertRaisesRegex(ValueError,'journal incomplete'):
            rollback_changes(self.c,run,apply=True)

    def test_interrupted_status_after_lost_lock_session(self):
        from app.factor_maintenance_control import start,connect
        old = start(self.c,'repair',{})
        self.c.close()
        with connect() as fresh:
            start(fresh,'sync',{})
            self.assertEqual(fresh.execute('SELECT status FROM quant.factor_maintenance_runs WHERE run_id=%s',(old,)).fetchone()['status'],'interrupted')

    def test_partial_committed_work_survives_failed_run_and_is_recoverable(self):
        from app.factor_maintenance_control import managed_run,rollback_changes
        from app.adjustment_factor_maintenance import AdjustmentFactorMaintenanceDependencies
        deps = AdjustmentFactorMaintenanceDependencies(None,None,None,None,None)
        async def fail_after_commit(scoped):
            with scoped.database.transaction() as conn:
                conn.execute("UPDATE quant.canonical_bars_daily SET adj_factor=8 WHERE symbol='000001.SZ'")
            raise RuntimeError('fixture credential should not be persisted')
        with self.assertRaises(RuntimeError):
            asyncio.run(managed_run(deps,'repair',fail_after_commit,{}))
        run = self.c.execute('SELECT * FROM quant.factor_maintenance_runs').fetchone()
        self.assertEqual(run['status'],'failed')
        self.assertNotIn('credential',str(run['result']))
        self.assertEqual(rollback_changes(self.c,run['run_id'],apply=True)['change_count'],1)


if __name__ == '__main__':
    unittest.main()
