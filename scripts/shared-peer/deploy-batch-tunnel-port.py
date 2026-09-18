"""Publish the peer-side batch database port (db-tunnel:5433) on lightServer.

Run as root ON THE PEER HOST, after the owner's batch tunnel task
(``trading-hareness-shared-peer-batch-tunnel``) is publishing
``127.0.0.1:15433``.  Modelled on ``deploy-private-tunnel.py``: bounded, backs
everything it touches up first, recreates **only** ``db-tunnel``, and restores
what it changed on any failure.

What it changes, and nothing else:

* ``.env``            - adds ``PEER_BATCH_DB_PORT`` / ``PEER_BATCH_REMOTE_PORT``
* ``compose.yaml``    - passes those two variables into ``db-tunnel`` (and, for
  the states that need it, ``PEER_LOCAL_BIND_ADDRESS``)
* ``ssh-tunnel-entrypoint.sh`` - gains the optional third ``-L`` forward (the
  image bakes the entrypoint, hence the rebuild)

It never writes the repo's entrypoint over a peer file it has not recognised.
The peer's deployed tree and this repository have drifted apart before, and the
two differ in exactly the place that matters: the peer's live entrypoint binds
its forwards to ``0.0.0.0`` literally, while the repo copy binds
``${PEER_LOCAL_BIND_ADDRESS:-127.0.0.1}`` and expects compose to set that
variable.  Overwriting one with the other on a peer whose compose does not set
it moves 5432/5681 into the sidecar's own network namespace: every sibling
container loses the database and the gateway, and a healthcheck that runs
*inside* that same container still passes.  So this script reads the peer's
ACTUAL deployed state first - the entrypoint's SHA-256, the rendered
``db-tunnel`` healthcheck, its environment keys and its image - and refuses
unless that state is one of ``KNOWN_PEER_STATES``.  ``compose.yaml`` is pinned
by its own LF-normalised SHA-256 rather than by its rendered form, because
``add_compose_environment`` rewrites the raw text at a literal anchor and every
reformatting of that one line renders identically: without the raw pin a
reformatted compose passes the gate and then dies mid-write.  Each known state
carries the
strategy it may be changed with: ``patch`` (insert the batch block into the
peer's own file, keeping its bind address) or ``repo_copy`` (write this repo's
entrypoint, and then ``PEER_LOCAL_BIND_ADDRESS`` must be present in, or is added
to, the compose environment).

Rollback: the running image is tagged ``:pre-batch-<stamp>`` **before** the
build, because ``docker compose build db-tunnel`` retags the image the running
container came from.  The printed rollback command restores the configuration
files *and* re-points that tag, which is the only reason rollback is a real
option rather than a promise.  The automatic rollback does the same, and - when
the failure happened after ``up -d`` - also recreates the container, because
retagging an image does nothing to a container that is already running from it.
The retag's exit code is kept and the recreated container's image id is read
back and compared with the preserved one (both are printed); only when they
match does the script re-probe 5432 and report the peer as back.

The verification runs from ``quant-research``, not from the sidecar: the
sidecar's image is ``openssh-client`` + ``netcat`` with no PostgreSQL client and
no database credentials, so a probe there can only ever fail.  ``quant-research``
has psycopg and the ``PG*`` environment, and being outside the sidecar is the
point - it proves the path the peer's containers actually use.  The probe is an
authenticated ``SELECT 1, inet_server_port()``: an open socket proves nothing
about what is behind it, and 55432 is the only answer that proves the owner's
PostgreSQL.  5432 is probed the same way before the change and again after the
recreate, so a batch port that works while the intraday path broke cannot be
reported as success.
"""
import hashlib
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
STAMP = time.strftime('%Y%m%dT%H%M%S')
BATCH_LOCAL_PORT = os.environ.get('PEER_BATCH_DB_PORT', '5433')
BATCH_REMOTE_PORT = os.environ.get('PEER_BATCH_REMOTE_PORT', '15433')
INTRADAY_LOCAL_PORT = '5432'
SERVICE = 'db-tunnel'
VERIFIER = 'quant-research'
NAMES = ['.env', 'compose.yaml', 'ssh-tunnel-entrypoint.sh']
BACKUP_ROOT = Path('/home/stockpeer/.local/share/trading-hareness/incident-backups')
C = D + ['compose', '--env-file', str(ROOT / '.env'), '-f', str(ROOT / 'compose.yaml'),
         '-f', str(ROOT / 'compose.intraday-owner.yaml')]

ENV_BLOCK = ('      PEER_BATCH_DB_PORT: ${PEER_BATCH_DB_PORT:-}\n'
             '      PEER_BATCH_REMOTE_PORT: ${PEER_BATCH_REMOTE_PORT:-15433}\n')
LOCAL_BIND_BLOCK = '      PEER_LOCAL_BIND_ADDRESS: "0.0.0.0"\n'
COMPOSE_ANCHOR = '      REMOTE_API_PORT: ${REMOTE_API_PORT:-15681}\n'
# The literal text every deployed entrypoint carries once the batch forward is
# really in it - both strategies produce it (``patch_entrypoint`` prefixes the
# peer's own bind address, the repo copy prefixes
# ``${PEER_LOCAL_BIND_ADDRESS:-127.0.0.1}``).  The mere STRING
# ``PEER_BATCH_DB_PORT`` is not enough: a half-finished edit, or a comment
# mentioning the variable, contains it too.
BATCH_FORWARD_MARKER = ':${PEER_BATCH_DB_PORT}:127.0.0.1:${PEER_BATCH_REMOTE_PORT:-15433}"'

# Every state this script is allowed to change, keyed by the SHA-256 of the
# peer's deployed ssh-tunnel-entrypoint.sh (LF-normalised).  Anything else is a
# refusal, not a guess: add the new state here, with its own strategy, after
# reading the peer's file.
#
# 'nc-legacy-20260917' is what lightServer is actually running (measured
# read-only on 2026-09-19: 1694 bytes, md5 257573f7..., healthcheck
# `nc -z 127.0.0.1 5432 && nc -z 127.0.0.1 5681`, db-tunnel has no `image:` key
# so compose builds and tags trading-hareness-peer-db-tunnel:latest, and the
# forwards are bound to 0.0.0.0 literally).  deploy-private-tunnel.py has NOT
# been applied to the peer's shared-peer stack: the richer
# /usr/local/bin/ssh-tunnel-healthcheck does not exist there.
#
# 'private-tunnel-repo' is the state the peer would be in after this
# repository's deploy/shared-peer/ tree is deployed as-is.  It is listed so the
# script keeps working after that happens; it has never been observed on the
# peer, and the script says so when it matches.
KNOWN_PEER_STATES = {
    'nc-legacy-20260917': {
        'entrypoint_sha256': 'b0aa1e02c60b7d0951e16b91250d863716ebaa554ee60ba0f18797469448072c',
        # The rendered compose is not enough to protect the literal text
        # rewrite: `REMOTE_API_PORT: ${REMOTE_API_PORT:-15681}`,
        # `REMOTE_API_PORT: "15681"`, a different indentation and a value from
        # an env_file all render identically, while add_compose_environment
        # anchors on the raw line and would die mid-write on a bare
        # AssertionError - after .env had already been rewritten. So the raw
        # file is pinned too, and a reformatted compose is the same printed
        # refusal as a drifted entrypoint.  (Measured read-only on the peer,
        # 2026-09-19: 4145 bytes, no CR bytes.)
        'compose_sha256': '39614927a2c6659724f1496659ca3cc126db8b9436bb7324689166b432019886',
        'healthcheck_test': ['CMD-SHELL', 'nc -z 127.0.0.1 5432 && nc -z 127.0.0.1 5681'],
        'environment_keys': ['PEER_SSH_HOST', 'PEER_SSH_HOST_KEY_ALIAS', 'PEER_SSH_PORT',
                             'PEER_SSH_USER', 'REMOTE_API_PORT', 'REMOTE_DB_PORT'],
        'image': None,
        'strategy': 'patch',
        # The peer's own file hardcodes 0.0.0.0 on both existing forwards; the
        # batch forward must match it, not introduce a variable the peer's
        # compose does not set.
        'batch_bind_address': '0.0.0.0',
        'entrypoint_anchor': 'exec ssh "$@" \\\n',
        'observed': True,
    },
    'private-tunnel-repo': {
        'entrypoint_sha256': 'ffee3be873f009d69e0ac46f5cdc32c25ab13f207b336a49b1ba56332d7f0a18',
        # deploy/shared-peer/compose.yaml as of ba717c8, the revision whose
        # entrypoint hashes to ffee3be8 - i.e. the pair that is deployed
        # together. Both hashes move together or this state is a new one.
        'compose_sha256': '0a32b45c756a8e60c85345419d430de8dfe0f6c68e1101ad0a50ad87c84e5689',
        'healthcheck_test': ['CMD', '/usr/local/bin/ssh-tunnel-healthcheck'],
        'environment_keys': ['PEER_LOCAL_BIND_ADDRESS', 'PEER_SSH_HOST', 'PEER_SSH_HOST_KEY_ALIAS',
                             'PEER_SSH_PORT', 'PEER_SSH_USER', 'PGDATABASE', 'PGPASSWORD',
                             'PGUSER', 'REMOTE_API_PORT', 'REMOTE_DB_PORT'],
        'image': 'trading-hareness-peer-db-tunnel:private-20260914',
        'strategy': 'repo_copy',
        'batch_bind_address': '${PEER_LOCAL_BIND_ADDRESS:-127.0.0.1}',
        'entrypoint_anchor': 'exec ssh -NT \\\n',
        'observed': False,
    },
}

# psycopg, not psql: the sidecar has no PostgreSQL client at all, and
# quant-research ships psycopg 3 (measured 3.2.6) rather than the psql binary.
# Credentials come from the container's own PG* environment and never appear on
# a command line or in this script's output.
PROBE = ("import psycopg;"
         "c=psycopg.connect(host='db-tunnel',port=%s,connect_timeout=5,"
         "options='-c statement_timeout=8000');"
         "r=c.execute('SELECT 1, inet_server_port()').fetchone();"
         "print('%%s|%%s' %% (r[0], r[1]))")


def config():
    return json.loads(subprocess.check_output(C + ['config', '--format', 'json']))


def compose_run(*arguments, **kwargs):
    return subprocess.run(C + list(arguments), check=True, **kwargs)


def sha256_text(text):
    return hashlib.sha256(text.replace('\r\n', '\n').encode('utf-8')).hexdigest()


def set_env_keys(text, updates):
    lines = [line for line in text.splitlines() if line.split('=', 1)[0] not in updates]
    lines.extend(key + '=' + value for key, value in updates.items())
    return '\n'.join(lines) + '\n'


def add_compose_environment(text, with_local_bind):
    """Insert the batch variables into db-tunnel's environment, once."""
    if 'PEER_BATCH_DB_PORT' in text:
        return text
    assert text.count(COMPOSE_ANCHOR) == 1, 'db-tunnel REMOTE_API_PORT anchor is not unique'
    block = ENV_BLOCK
    if with_local_bind and 'PEER_LOCAL_BIND_ADDRESS' not in text:
        # Only reachable on a repo_copy state: the repo entrypoint binds
        # ${PEER_LOCAL_BIND_ADDRESS:-127.0.0.1}, and 127.0.0.1 inside the
        # sidecar is invisible to every sibling container.
        block = LOCAL_BIND_BLOCK + block
    return text.replace(COMPOSE_ANCHOR, COMPOSE_ANCHOR + block, 1)


def patch_entrypoint(text, bind_address, anchor):
    """Add the optional batch forward to the peer's OWN entrypoint."""
    if 'PEER_BATCH_DB_PORT' in text:
        return text
    assert text.count(anchor) == 1, 'entrypoint exec anchor is not unique: ' + repr(anchor)
    block = (
        '\n'
        '# Optional third forward: the owner\'s batch tunnel (a separate SSH connection\n'
        '# on the owner side, reserved remote port 15433) so bulk/COPY jobs stop sharing\n'
        '# the intraday connection\'s TCP window. Unset means "not deployed" and this\n'
        '# container behaves exactly as before.\n'
        '#\n'
        '# A -L forward binds locally and does not require the far end to be listening,\n'
        '# so ExitOnForwardFailure=yes does not make the sidecar depend on the owner\'s\n'
        '# batch tunnel being up: if the owner side is down, connections to the batch\n'
        '# port fail individually while 5432/5681 keep working. That is also why the\n'
        '# healthcheck is deliberately left alone - gating container health on an\n'
        '# optimization would turn it into an outage.\n'
        '#\n'
        '# The bind address is copied from this file\'s existing forwards on purpose:\n'
        '# a socket bound to 127.0.0.1 lives in this container\'s network namespace and\n'
        '# is unreachable from the sibling containers that are supposed to use it.\n'
        'if [ -n "${PEER_BATCH_DB_PORT:-}" ]; then\n'
        '  set -- "$@" -L \\\n'
        '    "%s:${PEER_BATCH_DB_PORT}:127.0.0.1:${PEER_BATCH_REMOTE_PORT:-15433}"\n'
        'fi\n'
        '\n' % bind_address)
    return text.replace(anchor, block + anchor, 1)


def inspect_peer_state():
    """Read-only. Returns (state_name, known_state, rendered db-tunnel service)."""
    for name in NAMES + ['compose.intraday-owner.yaml']:
        assert (ROOT / name).exists(), 'missing peer file: ' + str(ROOT / name)
    entrypoint_text = (ROOT / 'ssh-tunnel-entrypoint.sh').read_text()
    entrypoint_hash = sha256_text(entrypoint_text)
    service = config()['services'][SERVICE]
    environment = service.get('environment') or {}
    observed = {
        'entrypoint_sha256': entrypoint_hash,
        'compose_sha256': sha256_text((ROOT / 'compose.yaml').read_text()),
        'healthcheck_test': list(service.get('healthcheck', {}).get('test') or []),
        'environment_keys': sorted(environment.keys()),
        'image': service.get('image'),
    }
    matches = [(key, value) for key, value in KNOWN_PEER_STATES.items()
               if value['entrypoint_sha256'] == entrypoint_hash]
    if not matches:
        # An unrecognised entrypoint has exactly one benign explanation: THIS
        # script already changed it.  Judge that here, after the known-state
        # lookup, and only on the evidence that a deploy actually landed - the
        # rendered VALUE of PEER_BATCH_DB_PORT is non-empty AND the entrypoint
        # carries the batch forward itself.
        #
        # The key's mere presence is not evidence of anything.  This
        # repository's own compose declares `PEER_BATCH_DB_PORT:
        # ${PEER_BATCH_DB_PORT:-}` unconditionally while .env.example ships the
        # variable commented out, so `docker compose config` renders the key
        # with an empty value on a peer where nothing has been deployed; a run
        # interrupted between the compose write and the entrypoint patch leaves
        # the same shape.  Both used to exit 0 reporting a deploy that never
        # happened.  Both now fall through to the refusal below.
        batch_value = str(environment.get('PEER_BATCH_DB_PORT') or '').strip()
        if batch_value and BATCH_FORWARD_MARKER in entrypoint_text:
            # Success, not a refusal: re-running to confirm idempotency, or
            # after a rollback was completed by hand, must not look like a
            # deploy failure to a wrapper reading the exit code.
            print('the batch port is already deployed on this peer; nothing to do:\n'
                  + json.dumps(dict(observed, batch_local_port=batch_value), indent=2))
            raise SystemExit(0)
        raise SystemExit(
            'refusing to touch the peer: its ssh-tunnel-entrypoint.sh is not a state this '
            'script knows how to change.\n'
            'observed: ' + json.dumps(observed, indent=2) + '\n'
            'supported entrypoint hashes: '
            + json.dumps({k: v['entrypoint_sha256'] for k, v in KNOWN_PEER_STATES.items()}, indent=2)
            + '\nRead the peer file, decide the strategy by hand, and add it to '
              'KNOWN_PEER_STATES before running this again.')
    name, known = matches[0]
    mismatch = {key: {'expected': known[key], 'observed': observed[key]}
                for key in ('compose_sha256', 'healthcheck_test', 'environment_keys', 'image')
                if known[key] != observed[key]}
    if mismatch:
        raise SystemExit(
            'refusing to touch the peer: its entrypoint matches known state %r but the rest '
            'of the deployed db-tunnel service does not.\n%s'
            % (name, json.dumps(mismatch, indent=2)))
    if not known['observed']:
        print('note: matched known state %r, which has never been observed on this peer.' % name,
              file=sys.stderr)
    return name, known


def container_id(service_name):
    return subprocess.check_output(C + ['ps', '-q', service_name], text=True).strip()


def probe_port(port):
    """Authenticated SELECT 1 through db-tunnel:<port>, from quant-research."""
    out = subprocess.check_output(
        C + ['exec', '-T', VERIFIER, 'python', '-c', PROBE % port],
        text=True, timeout=60).strip()
    assert out == '1|55432', (
        'db-tunnel:%s did not reach the owner database (expected 1|55432): %r' % (port, out))
    return out


def assert_preconditions():
    """Everything that can refuse must refuse BEFORE the first backup or write."""
    verifier = container_id(VERIFIER)
    assert verifier, (
        '%s is not running; it is the only container with a PostgreSQL client and the PG* '
        'environment, so without it the change cannot be verified' % VERIFIER)
    env_keys = set((config()['services'][VERIFIER].get('environment') or {}).keys())
    missing = sorted({'PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD'} - env_keys)
    assert not missing, '%s is missing the credentials to verify with: %s' % (VERIFIER, missing)
    subprocess.check_output(C + ['exec', '-T', VERIFIER, 'python', '-c', 'import psycopg'],
                            text=True, timeout=60)
    # The owner side must already be publishing the far end, or this deploy
    # produces a port that cannot work and will roll itself back after a
    # rebuild and a recreate of the live sidecar.
    listener = subprocess.run(['ss', '-ltn', 'sport = :' + BATCH_REMOTE_PORT],
                              capture_output=True, text=True)
    assert len(listener.stdout.strip().splitlines()) > 1, (
        'nothing is listening on 127.0.0.1:%s: install the owner-side batch tunnel task '
        '(install-shared-tunnel-task.ps1 -Profile batch) first' % BATCH_REMOTE_PORT)
    # Baseline: the intraday path works right now, so "5432 broke" after the
    # recreate can only mean this change broke it.
    baseline = probe_port(INTRADAY_LOCAL_PORT)
    return {'verifier_container': verifier, 'intraday_baseline': baseline}


def wait_healthy(timeout=180):
    deadline = time.time() + timeout
    container = container_id(SERVICE)
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


def main():
    if os.geteuid() != 0:
        raise SystemExit('run as root on the peer host')
    state_name, known = inspect_peer_state()
    preconditions = assert_preconditions()
    before = config()

    # `docker compose build db-tunnel` retags the image the running container
    # came from (this peer's compose has no `image:` key, so the built tag is
    # the project default and the known-good image becomes dangling and
    # untagged). Capture and pin it BEFORE the build, or there is nothing to
    # roll back to.
    running = json.loads(subprocess.check_output(
        D + ['inspect', container_id(SERVICE)], text=True))[0]
    running_image_ref = known['image'] or running['Config']['Image']
    running_image_id = running['Image']
    preserved_tag = running_image_ref.split(':')[0] + ':pre-batch-' + STAMP
    subprocess.run(D + ['tag', running_image_id, preserved_tag], check=True)
    batch_tag = running_image_ref.split(':')[0] + ':batch-' + STAMP

    backup = BACKUP_ROOT / (STAMP + '-batch-tunnel-port')
    backup.mkdir(parents=True, mode=0o700)
    for name in NAMES:
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, backup / name)
    rollback = ('cp %s/* %s/ && ' % (backup, ROOT)
                + ' '.join(D + ['tag', preserved_tag, running_image_ref]) + ' && '
                + ' '.join(C + ['up', '-d', '--no-deps', '--no-build', SERVICE]))
    built = False
    recreated = False
    try:
        (ROOT / '.env').write_text(set_env_keys((ROOT / '.env').read_text(), {
            'PEER_BATCH_DB_PORT': BATCH_LOCAL_PORT,
            'PEER_BATCH_REMOTE_PORT': BATCH_REMOTE_PORT,
        }))
        (ROOT / 'compose.yaml').write_text(add_compose_environment(
            (ROOT / 'compose.yaml').read_text(),
            with_local_bind=known['strategy'] == 'repo_copy'))
        if known['strategy'] == 'patch':
            (ROOT / 'ssh-tunnel-entrypoint.sh').write_text(patch_entrypoint(
                (ROOT / 'ssh-tunnel-entrypoint.sh').read_text(),
                known['batch_bind_address'], known['entrypoint_anchor']))
        else:
            (ROOT / 'ssh-tunnel-entrypoint.sh').write_text(
                (HERE / '..' / '..' / 'deploy' / 'shared-peer' / 'ssh-tunnel-entrypoint.sh')
                .resolve().read_text().replace('\r\n', '\n'))
        after = config()
        # Nothing but db-tunnel may move. The peer runs two writer profiles on
        # this stack; recreating quant-research here would be an outage.
        for name, rendered in before['services'].items():
            if name != SERVICE:
                assert rendered == after['services'][name], 'Unexpected service change: ' + name
        for key in ['volumes', 'ports', 'networks', 'restart', 'healthcheck']:
            assert before['services'][SERVICE].get(key) == after['services'][SERVICE].get(key), key
        assert after['services'][SERVICE].get('environment', {}).get('PEER_BATCH_DB_PORT') \
            == BATCH_LOCAL_PORT, 'the batch port did not reach the rendered db-tunnel environment'
        compose_run('build', SERVICE)
        built = True
        subprocess.run(D + ['tag', after['services'][SERVICE].get('image') or running_image_ref,
                            batch_tag], check=False)
        compose_run('up', '-d', '--no-deps', '--no-build', SERVICE)
        recreated = True
        container = wait_healthy()
        # Both paths, in this order: a batch port that works while the
        # intraday path broke is a failure, not a success.
        batch_query = probe_port(BATCH_LOCAL_PORT)
        intraday_query = probe_port(INTRADAY_LOCAL_PORT)
    except BaseException:
        for name in NAMES:
            if (backup / name).exists():
                shutil.copy2(backup / name, ROOT / name)
        retag = None
        if built:
            # Fire-and-forget used to be enough because nothing downstream
            # depended on it. It does now: the recreate below resolves
            # running_image_ref to whatever that tag points at, so a retag that
            # failed (the :pre-batch-<stamp> tag pruned by a concurrent
            # `docker image prune`, a daemon error, a full disk) would recreate
            # the container from the REJECTED image while this script printed
            # "rolled back". Keep the result and verify the outcome instead.
            retag = subprocess.run(D + ['tag', preserved_tag, running_image_ref], check=False)
        if recreated:
            # Restoring the files and re-pointing the tag is NOT a rollback once
            # `up -d` has run: the live container was created from the new image
            # and retagging :latest does not touch a running container. Every
            # failure that can reach here - wait_healthy timing out, the batch
            # probe failing, and above all the intraday probe failing, which is
            # the case the probe ordering exists to catch - would otherwise
            # leave lightServer serving the peer's database through an image
            # this script just decided was bad, while claiming it rolled back.
            # So recreate from the restored files and preserved tag, and say
            # plainly whether 5432 answers afterwards.
            print('failure after the container was recreated: restoring the previous container',
                  file=sys.stderr)
            back = subprocess.run(C + ['up', '-d', '--no-deps', '--no-build', SERVICE], check=False)
            # "up -d returned 0" only says compose did something; it does not
            # say WHICH image the container came from. Read it back and compare
            # with the image id that was preserved before the build, and print
            # both so the operator can check the claim rather than trust it.
            restored_image_id = None
            if back.returncode == 0:
                try:
                    restored_image_id = json.loads(subprocess.check_output(
                        D + ['inspect', container_id(SERVICE)], text=True))[0]['Image']
                except BaseException as inspect_error:   # noqa: BLE001 - reported below
                    restored_image_id = 'inspect failed: %s: %s' % (
                        type(inspect_error).__name__, inspect_error)
            print('preserved image id:  %s\nrecreated image id:  %s\nretag exit code:     %s'
                  % (running_image_id, restored_image_id,
                     'not attempted (no build ran)' if retag is None else retag.returncode),
                  file=sys.stderr)
            if back.returncode != 0 or restored_image_id != running_image_id:
                print('AUTOMATIC ROLLBACK FAILED: db-tunnel is NOT running the preserved image '
                      '(up -d exit %s, retag exit %s, expected image %s, got %s). The peer may '
                      'be serving from the NEW image. Run:\n  '
                      % (back.returncode,
                         'n/a' if retag is None else retag.returncode,
                         running_image_id, restored_image_id)
                      + rollback, file=sys.stderr)
            else:
                try:
                    print('rolled back; intraday probe: ' + probe_port(INTRADAY_LOCAL_PORT),
                          file=sys.stderr)
                except BaseException as probe_error:   # noqa: BLE001 - reported, never masks
                    print('ROLLED BACK BUT THE INTRADAY PATH IS STILL DOWN (%s: %s). The peer '
                          'has no database connection; investigate before anything else.'
                          % (type(probe_error).__name__, probe_error), file=sys.stderr)
        else:
            print('restored the previous configuration and image tag; the container was never '
                  'recreated, so nothing else changed on this peer.', file=sys.stderr)
        print('rollback command (idempotent, safe to re-run):\n  ' + rollback, file=sys.stderr)
        raise
    result = {'deployed': True, 'service': SERVICE, 'container': container, 'backup': str(backup),
              'peer_state': state_name, 'strategy': known['strategy'],
              'batch_local_port': BATCH_LOCAL_PORT, 'batch_remote_port': BATCH_REMOTE_PORT,
              'verifier': VERIFIER,
              'intraday_baseline': preconditions['intraday_baseline'],
              'verified_batch_select': batch_query,
              'verified_intraday_select': intraday_query,
              'previous_image_tag': preserved_tag, 'previous_image_id': running_image_id,
              'new_image_tag': batch_tag, 'image_ref': running_image_ref,
              'rollback': rollback}
    (HERE / 'batch-tunnel-deployment.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    print('\nrollback:\n  ' + rollback)


if __name__ == '__main__':
    main()
