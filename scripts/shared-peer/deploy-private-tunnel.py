"""One-incident private-route cutover; requires candidate acceptance first.

Run as root on lightServer. Only db-tunnel is recreated. Keep the existing
bridge topology and quant images; do NOT deploy a different local compose tree.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

D = ['sudo', '-u', 'stockpeer', 'env', 'XDG_RUNTIME_DIR=/run/user/1002',
     'DOCKER_HOST=unix:///run/user/1002/docker.sock', 'docker']
HERE = Path(__file__).resolve().parent
ROOT = Path('/home/stockpeer/trading_hareness/deploy/shared-peer')
TAG = 'trading-hareness-peer-db-tunnel:private-20260914'
C = D + ['compose', '--env-file', str(ROOT / '.env'), '-f', str(ROOT / 'compose.yaml'),
         '-f', str(ROOT / 'compose.intraday-owner.yaml')]


def config():
    return json.loads(subprocess.check_output(C + ['config', '--format', 'json']))


def main():
    assert json.loads((HERE / 'candidate-acceptance.json').read_text())['passed']
    before = config()
    names = ['.env', 'compose.yaml', 'ssh-tunnel-entrypoint.sh', 'Dockerfile.ssh-tunnel',
             'ssh-tunnel-healthcheck.sh']
    backup = Path('/home/stockpeer/.local/share/trading-hareness/incident-backups') / time.strftime('%Y%m%dT%H%M%S-private-tunnel')
    backup.mkdir(parents=True, mode=0o700)
    for name in names:
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, backup / name)
    identities = json.loads(subprocess.check_output(D + ['inspect',
        'trading-hareness-peer-quant-research-1', 'trading-hareness-peer-quant-research-scheduler-1']))
    identity = {i['Name']: {'id': i['Id'], 'pid': i['State']['Pid'], 'image': i['Image']} for i in identities}
    env = (ROOT / '.env').read_text().splitlines()
    updates = {'PEER_SSH_HOST': '172.29.57.199', 'PEER_SSH_HOST_KEY_ALIAS': '[47.110.79.189]:3535'}
    env = [line for line in env if line.split('=', 1)[0] not in updates]
    env.extend(k + '=' + v for k, v in updates.items())
    original = (ROOT / 'compose.yaml').read_text()
    head, tail = original.split('\n  quant-research:', 1)
    assert head.count('  db-tunnel:\n') == 1
    assert head.count('      PEER_SSH_USER: ${PEER_SSH_USER:-stockpeer}') == 1
    head = head.replace('  db-tunnel:\n', '  db-tunnel:\n    image: ' + TAG + '\n', 1)
    head = head.replace('      PEER_SSH_USER: ${PEER_SSH_USER:-stockpeer}',
        '      PEER_SSH_USER: ${PEER_SSH_USER:-stockpeer}\n'
        '      PEER_SSH_HOST_KEY_ALIAS: ${PEER_SSH_HOST_KEY_ALIAS:-}\n'
        '      PEER_LOCAL_BIND_ADDRESS: "0.0.0.0"\n'
        '      PGUSER: ${PEER_DB_USER:-stock_peer}\n'
        '      PGDATABASE: ${PEER_QUANT_DATABASE:-trading_hareness}\n'
        '      PGPASSWORD: ${PEER_DB_PASSWORD:?Set PEER_DB_PASSWORD}', 1)
    old = '      test: ["CMD-SHELL", "nc -z 127.0.0.1 5432 && nc -z 127.0.0.1 5681"]\n      interval: 10s\n      timeout: 3s\n      retries: 12'
    assert head.count(old) == 1
    head = head.replace(old, '      test: ["CMD", "/usr/local/bin/ssh-tunnel-healthcheck"]\n      interval: 15s\n      timeout: 10s\n      retries: 3\n      start_period: 10s', 1)
    try:
        (ROOT / '.env').write_text('\n'.join(env) + '\n')
        (ROOT / 'compose.yaml').write_text(head + '\n  quant-research:' + tail)
        # Keep future tunnel-only builds fixed too. Canonical sources, not the
        # generated FROM-baseline Dockerfile used for this immutable build.
        for name in ['ssh-tunnel-entrypoint.sh', 'ssh-tunnel-healthcheck.sh', 'Dockerfile.ssh-tunnel']:
            (ROOT / name).write_text((HERE / 'source' / name).read_text().replace('\r\n', '\n'))
        after = config()
        for name, service in before['services'].items():
            if name != 'db-tunnel':
                assert service == after['services'][name], 'Unexpected service change: ' + name
        for key in ['volumes', 'ports', 'networks', 'restart']:
            assert before['services']['db-tunnel'].get(key) == after['services']['db-tunnel'].get(key), key
        subprocess.run(C + ['up', '-d', '--no-deps', '--no-build', 'db-tunnel'], check=True)
    except BaseException:
        for name in names:
            if (backup / name).exists():
                shutil.copy2(backup / name, ROOT / name)
        raise
    result = {'deployed': True, 'image': TAG, 'backup': str(backup), 'quant_before': identity,
              'destination': updates['PEER_SSH_HOST'], 'host_key_identity': updates['PEER_SSH_HOST_KEY_ALIAS']}
    (HERE / 'deployment.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
