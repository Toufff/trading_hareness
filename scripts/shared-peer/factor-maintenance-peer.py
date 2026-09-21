#!/usr/bin/env python3
"""Versioned maintenance bundle; reuse peer credentials/deps without replacing services.

Run as stockpeer (rootless Docker). No bootstrap, migrations, scheduler, published
ports, upstream credentials or host secrets mounted into the one-shot container.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
CONTAINER = 'trading-hareness-peer-quant-research-1'
ENV_KEYS = frozenset({
    'PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'PGSSLMODE',
    'QUANT_SHARED_READ_API_BASE_URL', 'QUANT_SHARED_READ_API_KEY',
})


def launch_spec(inspected, root, arguments, *, cpu_quota_supported=False):
    """Pure launch plan: secret values live only in the child environment."""
    env = dict(os.environ)
    inherited = dict(item.split('=', 1) for item in inspected['Config']['Env'] if '=' in item)
    selected = {key: inherited[key] for key in ENV_KEYS if key in inherited}
    required = {'PGDATABASE', 'PGUSER', 'PGPASSWORD',
                'QUANT_SHARED_READ_API_BASE_URL', 'QUANT_SHARED_READ_API_KEY'}
    if any(not selected.get(key) for key in required):
        raise RuntimeError('reference container is missing shared database/gateway settings')
    selected.update(PGHOST='db-batch-tunnel', PGPORT='5433',
                    APP_GIT_SHA=f'factor-bundle:{root.name}', QUANT_FACTOR_ACTOR='stockpeer-maintainer',
                    QUANT_FACTOR_CPU_QUOTA='2' if cpu_quota_supported else 'unavailable_in_rootless_daemon',
                    PYTHONPATH='/opt/factor/quant-service', PYTHONDONTWRITEBYTECODE='1',
                    PYTHONUNBUFFERED='1')
    networks = inspected['NetworkSettings']['Networks']
    network = 'trading-hareness-peer_default'
    if network not in networks:
        raise RuntimeError('reference container is not on the expected peer network')
    if not inspected['State']['Running']:
        raise RuntimeError('reference container is stopped; inspect its configuration before proceeding')
    env.update(selected)
    cmd = ['docker', 'run', '--rm', '--init', '--network', network,
           '--label', 'stockplatform.role=factor-maintenance',
           '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m',
           '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
           '--pids-limit', '128', '--memory', '1g',
           '--mount', f'type=bind,src={root},dst=/opt/factor,readonly',
           '--workdir', '/opt/factor', '--entrypoint', 'python']
    if cpu_quota_supported:
        cmd.extend(['--cpus', '2'])
    for key in sorted(selected):
        cmd.extend(['-e', key])
    cmd.append(inspected['Image'])  # Immutable running image ID, not a moving tag.
    if arguments[0] == 'probe':
        cmd.extend(['/opt/factor/scripts/shared-peer/factor-maintenance-peer.py', '_probe'])
    else:
        cmd.extend(['/opt/factor/scripts/adjustment-factor-maintenance.py', *arguments,
                    '--env-file', '-'])
    return cmd, env


def probe():
    sys.path.insert(0, str(ROOT/'quant-service'))
    from app.factor_maintenance_control import connect
    from app.longhu_vendor_source import intraday_source
    import urllib.request
    with connect() as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            identity = conn.execute('SELECT current_user AS role,current_database() AS database').fetchone()
            version = conn.execute('SELECT version_num FROM quant.alembic_version').fetchone()
            tables = ('daily_adjustment_factors','canonical_bars_daily','market_bars_daily',
                      'factor_maintenance_runs','factor_maintenance_changes')
            grants = {table: all(conn.execute('SELECT has_table_privilege(current_user,%s,%s) AS ok',
                       ('quant.'+table, privilege)).fetchone()['ok']
                       for privilege in ('SELECT','INSERT','UPDATE','DELETE')) for table in tables}
    req = urllib.request.Request(os.environ['QUANT_SHARED_READ_API_BASE_URL'].rstrip('/')+'/api/v1/peer/contract',
        headers={'X-Quant-Read-Key': os.environ['QUANT_SHARED_READ_API_KEY']})
    with urllib.request.urlopen(req, timeout=30) as response:
        contract = json.load(response)
    report = dict(status='ready' if all(grants.values()) and contract.get('factor_maintenance') else 'failed',
                  **identity, schema=version['version_num'], grants=grants,
                  provider_class=type(intraday_source()).__name__,
                  contract_version=contract.get('contract_version',contract.get('version')),
                  factor_maintenance=contract.get('factor_maintenance'),
                  source_version=os.getenv('APP_GIT_SHA'), database_path='db-batch-tunnel:5433')
    report['resource_limits'] = dict(memory_mib=1024,pids=128,cpu_quota=os.getenv('QUANT_FACTOR_CPU_QUOTA'))
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report['status']=='ready' else 1


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ['_probe']:
        return probe()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['probe','status','sync','repair','validate','history','rollback'])
    args, rest = parser.parse_known_args(arguments)
    # Disallow overriding inherited secrets with a Windows/host env-file path.
    if any(value == '--env-file' or value.startswith('--env-file=') for value in rest):
        parser.error('the launcher always uses inherited private environment')
    inspected = json.loads(subprocess.check_output(['docker','inspect',CONTAINER]))[0]
    cpu_quota = json.loads(subprocess.check_output(['docker','info','--format','{{json .CPUCfsQuota}}']))
    command, env = launch_spec(inspected, ROOT, [args.command,*rest], cpu_quota_supported=cpu_quota)
    return subprocess.run(command, env=env, check=False).returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status':'failed','error_class':type(exc).__name__,
                          'reason':'peer maintenance launcher failed; verify container, batch tunnel and schema'}))
        raise SystemExit(1)
