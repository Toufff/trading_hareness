"""One-incident deployment, preserving the running peer settings and volumes.

Build/test first. Run as root on lightServer. Save private rollback files outside
the repository, persist two previously ephemeral settings, replace only the two
quant images. Never recreate the database/tunnel or run migrations.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

D = ['sudo', '-u', 'stockpeer', 'env', 'XDG_RUNTIME_DIR=/run/user/1002',
     'DOCKER_HOST=unix:///run/user/1002/docker.sock', 'docker']
ROOT = Path('/home/stockpeer/trading_hareness/deploy/shared-peer')
TAG = 'trading-hareness-peer-quant-research:pool-recovery-20260914'
SERVICES = ['quant-research', 'quant-research-scheduler']
C = D + ['compose', '--env-file', str(ROOT / '.env'), '-f', str(ROOT / 'compose.yaml'),
         '-f', str(ROOT / 'compose.intraday-owner.yaml')]


def out(command):
    return subprocess.check_output(command, text=True)


def main():
    config = json.loads(out(C + ['config', '--format', 'json']))
    running = {s: json.loads(out(D + ['inspect', 'trading-hareness-peer-' + s + '-1']))[0] for s in SERVICES}
    for s in SERVICES:
        env = dict(item.split('=', 1) for item in running[s]['Config']['Env'])
        differences = {k for k, v in config['services'][s]['environment'].items() if str(v) != env.get(k)}
        allowed = {'QUANT_BACKGROUND_TASKS_ENABLED', 'QUANT_RUNTIME_PROFILE'} if s == 'quant-research' else set()
        assert differences <= allowed, sorted(differences)
    backup = Path('/home/stockpeer/.local/share/trading-hareness/incident-backups') / time.strftime('%Y%m%dT%H%M%S-pool')
    backup.mkdir(parents=True, mode=0o700)
    for name in ['.env', 'compose.intraday-owner.yaml']:
        shutil.copy2(ROOT / name, backup / name)
    (backup / 'running.json').write_text(json.dumps(running))
    os.chmod(backup / 'running.json', 0o600)
    env = dict(item.split('=', 1) for item in running['quant-research']['Config']['Env'])
    desired = {'PEER_BACKGROUND_TASKS_ENABLED': env['QUANT_BACKGROUND_TASKS_ENABLED'],
               'PEER_RUNTIME_PROFILE': env['QUANT_RUNTIME_PROFILE']}
    lines = (ROOT / '.env').read_text().splitlines()
    lines = [line for line in lines if line.split('=', 1)[0] not in desired]
    lines.extend(f'{k}={v}' for k, v in desired.items())
    override = (ROOT / 'compose.intraday-owner.yaml').read_text()
    assert override.count('  quant-research:\n') == 1
    assert override.count('    image: trading-hareness-peer-quant-research:latest') == 1
    override = override.replace('  quant-research:\n', '  quant-research:\n    image: ' + TAG + '\n', 1)
    override = override.replace('    image: trading-hareness-peer-quant-research:latest', '    image: ' + TAG, 1)
    try:
        (ROOT / '.env').write_text('\n'.join(lines) + '\n')
        (ROOT / 'compose.intraday-owner.yaml').write_text(override)
        updated = json.loads(out(C + ['config', '--format', 'json']))
        for s in SERVICES:
            actual_env = dict(item.split('=', 1) for item in running[s]['Config']['Env'])
            assert all(str(v) == actual_env.get(k) for k, v in updated['services'][s]['environment'].items())
            for key in ['ports', 'volumes', 'networks', 'command', 'entrypoint']:
                assert config['services'][s].get(key) == updated['services'][s].get(key), key
        subprocess.run(C + ['up', '-d', '--no-deps', '--no-build', *SERVICES], check=True)
    except BaseException:
        for name in ['.env', 'compose.intraday-owner.yaml']:
            shutil.copy2(backup / name, ROOT / name)
        raise
    print(json.dumps({'deployed': SERVICES, 'image': TAG, 'private_rollback': str(backup),
                      'persisted_runtime': desired, 'database_and_tunnel_untouched': True}))


if __name__ == '__main__':
    main()
