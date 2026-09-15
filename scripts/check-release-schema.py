"""Read-only rollback guard: candidate code must recognize the live schema head."""
import argparse
import json
from pathlib import Path
import sys


def recognizes_revision(runtime, revision):
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    config = Config()
    config.set_main_option('script_location', str(Path(runtime) / 'quant-service' / 'migrations'))
    try:
        return bool(ScriptDirectory.from_config(config).get_revision(revision))
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-runtime', required=True)
    parser.add_argument('--env-file', default=r'G:\StockPlatform\config\runtime.env')
    args = parser.parse_args()
    import psycopg
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'quant-service'))
    from app.db_dsn import connection_params
    config = dict(line.split('=', 1) for line in Path(args.env_file).read_text(encoding='utf-8-sig').splitlines()
                  if '=' in line and not line.startswith('#'))
    with psycopg.connect(**connection_params(config), options='-c default_transaction_read_only=on -c statement_timeout=5000') as db:
        revisions = [row[0] for row in db.execute('SELECT version_num FROM quant.alembic_version')]
    compatible = bool(revisions) and all(recognizes_revision(args.candidate_runtime, revision) for revision in revisions)
    print(json.dumps({'compatible': compatible, 'database_revisions': revisions}))
    return 0 if compatible else 2


if __name__ == '__main__':
    raise SystemExit(main())
