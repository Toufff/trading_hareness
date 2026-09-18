"""Publish the peer-side batch database port (db-tunnel:5433) on lightServer.

Run as root ON THE PEER HOST, after the owner's batch tunnel task
(``trading-hareness-shared-peer-batch-tunnel``) is publishing
``127.0.0.1:15433``.  Modelled on ``deploy-private-tunnel.py``: bounded, backs
everything it touches up first, recreates **only** ``db-tunnel``, and restores
the backup on any failure.

What it changes, and nothing else:

* ``.env``            - adds ``PEER_BATCH_DB_PORT`` / ``PEER_BATCH_REMOTE_PORT``
* ``compose.yaml``    - passes those two variables into ``db-tunnel``
* ``ssh-tunnel-entrypoint.sh`` - the reviewed version with the optional third
  ``-L`` forward (the image bakes the entrypoint, hence the rebuild)

Why a second connection at all: SSH multiplexes every forward over one TCP
connection, so the existing ``-L 5432`` and a hypothetical ``-L 5433`` on the
same sidecar connection would still share one window and one queue.  The owner
side therefore dials a *separate* SSH connection for 15433, and this script
only exposes its far end to the peer's containers.

The verification is an authenticated ``SELECT 1`` through 5433 that also
reports ``inet_server_port()``: an open socket proves nothing about what is
behind it, and 55432 is the only answer that proves the owner's PostgreSQL.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

D = ['sudo', '-u', 'stockpeer', 'env', 'XDG_RUNTIME_DIR=/run/user/1002',
     'DOCKER_HOST=unix:///run/user/1002/docker.sock', 'docker']
HERE = Path(__file__).resolve().parent
ROOT = Path('/home/stockpeer/trading_hareness/deploy/shared-peer')
TAG = 'trading-hareness-peer-db-tunnel:batch-20260919'
BATCH_LOCAL_PORT = os.environ.get('PEER_BATCH_DB_PORT', '5433')
BATCH_REMOTE_PORT = os.environ.get('PEER_BATCH_REMOTE_PORT', '15433')
SERVICE = 'db-tunnel'
NAMES = ['.env', 'compose.yaml', 'ssh-tunnel-entrypoint.sh']
C = D + ['compose', '--env-file', str(ROOT / '.env'), '-f', str(ROOT / 'compose.yaml'),
         '-f', str(ROOT / 'compose.intraday-owner.yaml')]

ENV_BLOCK = ('      PEER_BATCH_DB_PORT: ${PEER_BATCH_DB_PORT:-}\n'
             '      PEER_BATCH_REMOTE_PORT: ${PEER_BATCH_REMOTE_PORT:-15433}\n')


def config():
    return json.loads(subprocess.check_output(C + ['config', '--format', 'json']))


def compose_run(*arguments, **kwargs):
    return subprocess.run(C + list(arguments), check=True, **kwargs)


def set_env_keys(text, updates):
    lines = [line for line in text.splitlines() if line.split('=', 1)[0] not in updates]
    lines.extend(key + '=' + value for key, value in updates.items())
    return '\n'.join(lines) + '\n'


def add_compose_environment(text):
    """Insert the two variables into db-tunnel's environment, once."""
    if 'PEER_BATCH_DB_PORT' in text:
        return text
    anchor = '      REMOTE_API_PORT: ${REMOTE_API_PORT:-15681}\n'
    assert text.count(anchor) == 1, 'db-tunnel REMOTE_API_PORT anchor is not unique'
    return text.replace(anchor, anchor + ENV_BLOCK, 1)


def wait_healthy(timeout=180):
    deadline = time.time() + timeout
    container = subprocess.check_output(C + ['ps', '-q', SERVICE], text=True).strip()
    assert container, 'db-tunnel container id not found after recreate'
    while time.time() < deadline:
        state = json.loads(subprocess.check_output(D + ['inspect', container], text=True))[0]['State']
        status = state.get('Health', {}).get('Status', state['Status'])
        if status == 'healthy':
            return container
        if state['Status'] == 'exited':
            raise SystemExit('db-tunnel exited during recreate: ' + json.dumps(state)[:400])
        time.sleep(3)
    raise SystemExit('db-tunnel did not become healthy within %ds' % timeout)


def verify_batch_port():
    """Authenticated SELECT 1 through 5433, inside the sidecar."""
    probe = ("PGPORT=%s PGCONNECT_TIMEOUT=5 PGOPTIONS='-c statement_timeout=8000' "
             "psql -X -w -A -t -v ON_ERROR_STOP=1 "
             "-c 'SELECT 1, inet_server_port()'" % BATCH_LOCAL_PORT)
    out = subprocess.check_output(
        C + ['exec', '-T', SERVICE, 'sh', '-c', 'timeout 20 env ' + probe], text=True).strip()
    assert out == '1|55432', 'batch port did not reach the owner database: ' + repr(out)
    return out


def main():
    if os.geteuid() != 0:
        raise SystemExit('run as root on the peer host')
    before = config()
    backup = (Path('/home/stockpeer/.local/share/trading-hareness/incident-backups') /
              time.strftime('%Y%m%dT%H%M%S-batch-tunnel-port'))
    backup.mkdir(parents=True, mode=0o700)
    for name in NAMES:
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, backup / name)
    rollback = ('cp %s/* %s/ && ' % (backup, ROOT)) + ' '.join(
        C + ['up', '-d', '--no-deps', '--no-build', SERVICE])
    try:
        (ROOT / '.env').write_text(set_env_keys((ROOT / '.env').read_text(), {
            'PEER_BATCH_DB_PORT': BATCH_LOCAL_PORT,
            'PEER_BATCH_REMOTE_PORT': BATCH_REMOTE_PORT,
        }))
        (ROOT / 'compose.yaml').write_text(add_compose_environment((ROOT / 'compose.yaml').read_text()))
        (ROOT / 'ssh-tunnel-entrypoint.sh').write_text(
            (HERE / '..' / '..' / 'deploy' / 'shared-peer' / 'ssh-tunnel-entrypoint.sh')
            .resolve().read_text().replace('\r\n', '\n'))
        after = config()
        # Nothing but db-tunnel may move. The peer runs two writer profiles on
        # this stack; recreating quant-research here would be an outage.
        for name, service in before['services'].items():
            if name != SERVICE:
                assert service == after['services'][name], 'Unexpected service change: ' + name
        for key in ['volumes', 'ports', 'networks', 'restart']:
            assert before['services'][SERVICE].get(key) == after['services'][SERVICE].get(key), key
        # The entrypoint is baked into the image, so this one service is
        # rebuilt under a new tag; the previous tag stays intact for rollback.
        compose_run('build', SERVICE)
        subprocess.run(D + ['tag', after['services'][SERVICE]['image'], TAG], check=False)
        compose_run('up', '-d', '--no-deps', '--no-build', SERVICE)
        container = wait_healthy()
        query = verify_batch_port()
    except BaseException:
        for name in NAMES:
            if (backup / name).exists():
                shutil.copy2(backup / name, ROOT / name)
        print('restored the previous configuration; re-run the rollback command '
              'if the container was already recreated:\n  ' + rollback, file=sys.stderr)
        raise
    result = {'deployed': True, 'service': SERVICE, 'container': container, 'backup': str(backup),
              'batch_local_port': BATCH_LOCAL_PORT, 'batch_remote_port': BATCH_REMOTE_PORT,
              'verified_select': query, 'rollback': rollback}
    (HERE / 'batch-tunnel-deployment.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    print('\nrollback:\n  ' + rollback)


if __name__ == '__main__':
    main()
