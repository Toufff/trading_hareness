"""Refuse publishing a source tree that would discard an active release commit."""
import argparse
import json
from pathlib import Path
import subprocess


def verify(source: Path, platform: Path, expected_head: str):
    def git(*args):
        return subprocess.run(['git', '-C', str(source), *args], capture_output=True, text=True, check=False)

    actual = git('rev-parse', 'HEAD')
    if actual.returncode or actual.stdout.strip() != expected_head:
        raise ValueError('source HEAD changed during release; restart from a reviewed clean commit')
    state_path = platform / 'release-state.json'
    state = json.loads(state_path.read_text(encoding='utf-8-sig')) if state_path.exists() else {}
    active = state.get('active_release')
    manifest_path = platform / 'current' / 'release-manifest.json'
    if not active and not manifest_path.exists():
        return {'status': 'initial_publish', 'source_commit': expected_head}
    if not active or not manifest_path.exists():
        raise ValueError('active release identity is incomplete; cannot prove publish preserves production')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    previous = manifest.get('git_head')
    if manifest.get('release_id') != active or not previous or manifest.get('dirty') is not False:
        raise ValueError('active release identity is inconsistent or dirty; reconcile before publishing')
    if git('merge-base', '--is-ancestor', previous, expected_head).returncode != 0:
        raise ValueError('source does not contain active production commit; merge deployed work first, or use the explicit rollback workflow')
    return {'status': 'preserves_active_commit', 'active_commit': previous, 'source_commit': expected_head}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--platform', type=Path, required=True)
    parser.add_argument('--expected-head', required=True)
    args = parser.parse_args()
    try:
        result = verify(args.source, args.platform, args.expected_head)
    except (ValueError, OSError) as error:
        print(json.dumps({'status': 'refused', 'reason': str(error)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
