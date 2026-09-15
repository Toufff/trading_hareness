"""Build a minimal peer hotfix from the RUNNING baseline (no deployment).

Run on lightServer as root with helper and regression script next to this file.
The generated context contains source only. No credentials leave Docker inspect.
Exact replacement counts deliberately abort on unexpected source versions.
"""
import hashlib
import json
from pathlib import Path
import subprocess

D = ['sudo', '-u', 'stockpeer', 'env', 'XDG_RUNTIME_DIR=/run/user/1002',
     'DOCKER_HOST=unix:///run/user/1002/docker.sock', 'docker']
HERE = Path(__file__).resolve().parent
CONTAINER = 'trading-hareness-peer-quant-research-1'
TAG = 'trading-hareness-peer-quant-research:pool-recovery-20260914'


def docker(*args):
    return subprocess.check_output([*D, *args], text=True)


def replace_once(text, before, after):
    assert text.count(before) == 1, f'Unexpected source anchor: {before[:90]}'
    return text.replace(before, after, 1)


def main():
    info = json.loads(docker('inspect', CONTAINER))[0]
    baseline = info['Image']
    baseline_tag = 'trading-hareness-peer-quant-research:before-pool-recovery-20260914'
    docker('tag', baseline, baseline_tag)
    context = HERE / 'context'
    (context / 'app' / 'routers').mkdir(parents=True, exist_ok=True)
    paths = ['app/database.py', 'app/main.py', 'app/routers/system_control.py']
    hashes = {}
    for path in paths:
        source = docker('exec', CONTAINER, 'cat', '/app/' + path)
        hashes[path] = {'before': hashlib.sha256(source.encode()).hexdigest()}
        if path == 'app/database.py':
            source = replace_once(source, 'from psycopg.rows import dict_row',
                                  'from psycopg.rows import dict_row\nfrom .empty_pool_recovery import EmptyPoolRecovery')
            # Unique to AsyncDatabase; don't touch the synchronous pool.
            marker = 'class AsyncDatabase:'
            assert source.count(marker) == 1
            prefix, suffix = source.split(marker)
            suffix = replace_once(suffix, '        self._opened = False\n\n    async def open',
                                  '        self._empty_pool_recovery = EmptyPoolRecovery()\n        self._opened = False\n\n    async def open')
            suffix = replace_once(suffix, '        await self.open()\n        async with self._pool.connection()',
                                  '        await self.open()\n        await self._empty_pool_recovery.ensure_progress(self._pool)\n        async with self._pool.connection()')
            source = prefix + marker + suffix
        elif path == 'app/main.py':
            source = replace_once(source, '    health_payload=_health_payload,',
                                  '    health_payload=_health_payload,\n    async_database_probe=async_db.ping,')
        else:
            source = replace_once(source, 'from dataclasses import dataclass', 'import asyncio\nfrom dataclasses import dataclass')
            source = replace_once(source, 'from typing import Any, Callable', 'from typing import Any, Awaitable, Callable')
            source = replace_once(source, 'from fastapi.responses import Response',
                                  'from fastapi.responses import Response\nfrom starlette.concurrency import run_in_threadpool')
            source = replace_once(source, '    legacy_bootstrap: Callable[[], dict[str, Any]]',
                                  '    legacy_bootstrap: Callable[[], dict[str, Any]]\n    async_database_probe: Callable[[], Awaitable[None]] | None = None')
            source = replace_once(source, '    def health() -> dict[str, Any]:\n        try:\n            return deps.health_payload()',
                                  '    async def health() -> dict[str, Any]:\n        if deps.async_database_probe is not None:\n            try:\n                await asyncio.wait_for(deps.async_database_probe(), timeout=1.0)\n            except Exception as error:\n                raise HTTPException(status_code=503, detail="async database unavailable") from error\n        try:\n            return await run_in_threadpool(deps.health_payload)')
            source = replace_once(source, 'detail=f"database unavailable: {error}"', 'detail="database unavailable"')
        compile(source, path, 'exec')
        (context / path).write_text(source)
        hashes[path]['after'] = hashlib.sha256(source.encode()).hexdigest()
    helper = (HERE / 'empty_pool_recovery.py').read_text()
    (context / 'app' / 'empty_pool_recovery.py').write_text(helper)
    (context / 'Dockerfile').write_text(
        f'FROM {baseline_tag}\nCOPY app/database.py app/main.py app/empty_pool_recovery.py /app/app/\n'
        'COPY app/routers/system_control.py /app/app/routers/\n')
    subprocess.run([*D, 'build', '--network=none', '-t', TAG, str(context)], check=True)
    result = {'baseline': baseline, 'image': docker('image', 'inspect', TAG, '--format', '{{.Id}}').strip(),
              'tag': TAG, 'source_hashes': hashes}
    (HERE / 'build-evidence.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
