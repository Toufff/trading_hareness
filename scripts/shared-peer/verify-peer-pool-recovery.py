"""Explicit maintenance test: interrupt ONLY peer db-tunnel, verify same PIDs recover.

Not a scheduled task. A detached 20-second restore also runs as a fail-safe.
The Windows database/API remain running. Output contains no credentials.
"""
import concurrent.futures
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

D = ['sudo', '-u', 'stockpeer', 'env', 'XDG_RUNTIME_DIR=/run/user/1002',
     'DOCKER_HOST=unix:///run/user/1002/docker.sock', 'docker']
NAMES = ['trading-hareness-peer-quant-research-1', 'trading-hareness-peer-quant-research-scheduler-1']
TUNNEL = 'trading-hareness-peer-db-tunnel-1'
PATHS = ['/health', '/api/v1/providers/health', '/api/v1/strategy/health',
         '/api/v1/intraday/services/status', '/api/v1/automation/runs?limit=1']


def identity():
    result = {}
    for name in NAMES:
        item = json.loads(subprocess.check_output(D + ['inspect', name]))[0]
        result[name] = {'id': item['Id'], 'pid': item['State']['Pid'],
                        'restart_count': item['RestartCount'], 'image': item['Image']}
    return result


def request(port, path):
    started = time.monotonic()
    result = {'port': port, 'path': path}
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=6) as response:
            value = json.load(response)
            result['http'] = response.status
            if path == '/health':
                result['async_pool'] = value.get('async_database_pool')
                result['loops'] = value.get('runtime_loops')
    except urllib.error.HTTPError as error:
        result['http'] = error.code
        result['detail'] = error.read().decode()[:200]
    except Exception as error:
        result['http'] = 0
        result['error_type'] = type(error).__name__
    result['seconds'] = round(time.monotonic() - started, 3)
    return result


def healths():
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(lambda port: request(port, '/health'), [15682, 15683]))


def main():
    deadline = time.monotonic() + 90
    while True:
        before_health = healths()
        if all(item['http'] == 200 for item in before_health):
            break
        assert time.monotonic() < deadline, before_health
        time.sleep(2)
    before = identity()
    print('Both applications ready; beginning bounded tunnel interruption.', flush=True)
    restore_code = 'import time,subprocess;time.sleep(20);subprocess.run(' + repr(D + ['start', TUNNEL]) + ')'
    failsafe = subprocess.Popen(['python3', '-c', restore_code], start_new_session=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        subprocess.run(D + ['stop', '-t', '1', TUNNEL], check=True, timeout=10, capture_output=True)
        during = healths()
        print(json.dumps({'during_outage': during}), flush=True)
    finally:
        subprocess.run(D + ['start', TUNNEL], check=True, timeout=15, capture_output=True)
        failsafe.terminate()
        failsafe.wait(timeout=5)
    restored_at = time.monotonic()
    attempts = []
    while True:
        rows = healths()
        attempts.append(rows)
        if all(row['http'] == 200 for row in rows):
            break
        assert time.monotonic() - restored_at < 90, rows
        time.sleep(2)
    recovered_seconds = round(time.monotonic() - restored_at, 3)
    endpoints = [request(port, path) for port in [15682, 15683] for path in PATHS]
    after = identity()
    result = {'passed': before == after and all(r['http'] == 503 for r in during)
              and all(r['http'] == 200 for r in endpoints),
              'before': before, 'during_outage': during, 'after': after,
              'recovered_seconds': recovered_seconds, 'endpoints': endpoints,
              'recovery_attempts': attempts}
    (Path(__file__).resolve().parent / 'deployed-outage-recovery.json').write_text(json.dumps(result, indent=2))
    print(json.dumps({'passed': result['passed'], 'same_application_processes': before == after,
                      'recovered_seconds': recovered_seconds,
                      'endpoint_codes': [r['http'] for r in endpoints]}), flush=True)
    assert result['passed'], result


if __name__ == '__main__':
    main()
