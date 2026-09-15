"""Maintenance acceptance for the isolated private-route tunnel candidate.

Run on lightServer as root after starting stock-tunnel-candidate-20260914.
Only this explicitly named candidate is interrupted. Production is untouched.
"""
import json
from pathlib import Path
import subprocess
import time

D = ['sudo', '-u', 'stockpeer', 'env', 'XDG_RUNTIME_DIR=/run/user/1002',
     'DOCKER_HOST=unix:///run/user/1002/docker.sock', 'docker']
NAME = 'stock-tunnel-candidate-20260914'
ROOT = Path(__file__).resolve().parent


def inspect():
    return json.loads(subprocess.check_output(D + ['inspect', NAME]))[0]


def health(extra=()):
    return subprocess.run(D + ['exec', *extra, NAME, '/usr/local/bin/ssh-tunnel-healthcheck'],
                          capture_output=True, text=True, timeout=12)


def ready():
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if health().returncode == 0:
            return
        time.sleep(1)
    raise AssertionError('Candidate did not recover before deadline')


def main():
    result = {'passed': False, 'tests': []}
    try:
        ready()
        for extra in [('-e', 'PGPASSWORD=deliberately-wrong-probe-password'),
                      ('-e', 'PGDATABASE=deliberately_missing_probe_database')]:
            r = health(extra)
            assert r.returncode != 0 and 'postgresql_query_failed' in r.stdout
            assert 'deliberately-' not in r.stdout + r.stderr
            result['tests'].append({'test': extra[1].split('=')[0], 'passed': True})
        print('Real healthcheck rejects wrong password and missing database.', flush=True)
        for cycle in range(3):
            before = inspect()['RestartCount']
            started = time.monotonic()
            # Docker may kill the exec process together with PID 1 before it
            # reports success. Verify the restart and query, not exec's race.
            stopped = subprocess.run(D + ['exec', NAME, 'kill', '-TERM', '1'], timeout=5)
            assert stopped.returncode in (0, 137, 143), stopped.returncode
            deadline = time.monotonic() + 20
            while inspect()['RestartCount'] == before and time.monotonic() < deadline:
                time.sleep(0.5)
            assert inspect()['RestartCount'] > before
            ready()
            row = {'test': 'ssh_process_exit', 'cycle': cycle + 1, 'passed': True,
                   'recovery_seconds': round(time.monotonic() - started, 3)}
            result['tests'].append(row)
            print(json.dumps(row), flush=True)
        script = (ROOT / 'verify-tunnel-load.py').read_text()
        r = subprocess.run(D + ['exec', '-i', '-e', 'TEST_PGHOST=' + NAME,
                               'trading-hareness-peer-quant-research-1', 'timeout', '75', 'python', '-'],
                           input=script, capture_output=True, text=True, timeout=90)
        (ROOT / 'candidate-load.jsonl').write_text(r.stdout)
        r.check_returncode()
        summary = json.loads(r.stdout.strip().splitlines()[-1])
        assert summary['passed']
        result['tests'].append({'test': 'concurrent_bulk_and_short_queries', **summary})
        result['passed'] = True
        print(json.dumps(summary), flush=True)
    finally:
        (ROOT / 'candidate-acceptance.json').write_text(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
