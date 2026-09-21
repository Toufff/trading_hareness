"""Run selected database tests in a newly created disposable owner-cluster database.

Only PG connection settings are inherited from runtime.env; service/model/alert
settings are not. Never runs test fixtures against the production database.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import uuid
import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'quant-service'))
from app.db_dsn import connection_params


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', default='G:/StockPlatform/config/runtime.env')
    parser.add_argument('tests', nargs='+')
    args = parser.parse_args()
    cfg = {}
    for line in Path(args.env_file).read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            if key.strip() in {'PGHOST', 'PGPORT', 'PGUSER', 'PGPASSWORD', 'PGDATABASE', 'PGADMINUSER', 'PGADMINPASSWORD'}:
                cfg[key.strip()] = value.strip()
    name = 'stock_audit_test_' + uuid.uuid4().hex[:12]
    params = connection_params(cfg)
    prod_name = params['dbname']
    owner = params['user']
    admin_params = {**params, 'user': cfg.get('PGADMINUSER') or owner,
                    'password': cfg.get('PGADMINPASSWORD') or params['password']}
    assert name != prod_name and name.startswith('stock_audit_test_')
    with psycopg.connect(**admin_params, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {} OWNER {} TEMPLATE template0').format(sql.Identifier(name), sql.Identifier(owner)))
        try:
            env = {k:v for k,v in os.environ.items() if not k.startswith(('PG', 'QUANT_', 'FEISHU_', 'DEEPSEEK_', 'TYPESAFE_'))}
            env.update({k:v for k,v in cfg.items() if not k.startswith('PGADMIN')}, PGDATABASE=name, PYTHONIOENCODING='utf-8')
            subprocess.run(['node', 'feishu-adapter/initialize-ledger.mjs'], cwd=ROOT, env=env, check=True)
            subprocess.run([sys.executable, 'database_bootstrap.py'],
                           cwd=ROOT/'quant-service', env=env, check=True)
            test_runner = (
                'import sys,pytest; result=pytest.main(sys.argv[1:]); '
                'module=sys.modules.get("app.main"); '
                'db=getattr(module,"db",None); db.close() if db else None; '
                'raise SystemExit(result)'
            )
            result = subprocess.run([sys.executable, '-c', test_runner, *args.tests, '-q'],
                                    cwd=ROOT/'quant-service', env=env, timeout=600)
            print(f'isolated_database={name} test_exit={result.returncode}', flush=True)
            return result.returncode
        finally:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


if __name__ == '__main__':
    raise SystemExit(main())
