"""Announce a deployment to external consumers, and gate it on the clock.

``publish-stock-release.ps1`` calls this three times: once to ask whether the
window is open, once to announce ``starting`` before it touches anything, and
once to close the deploy out.  Each call prints one JSON receipt.

Exit codes:

* ``0``  the receipt is the answer (including an announcement that could not be
  written -- see ``recorded``).  **Announcing never fails a deploy**; a release
  blocked by its own bookkeeping is worse than an unannounced one.
* ``3``  ``check-window`` only: the window is closed and ``--force`` was not
  given.  This is the one refusal that is meant to stop a publish.
* ``2``  the arguments themselves were wrong.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'quant-service'))
from dotenv import load_dotenv  # noqa: E402

DEFAULT_ENV_FILE = 'G:/StockPlatform/config/runtime.env'


def _print(payload):
    stream = sys.stdout
    reconfigure = getattr(stream, 'reconfigure', None)
    if reconfigure is not None:
        reconfigure(encoding='utf-8')
    print(json.dumps(payload, ensure_ascii=False, default=str))


def _surfaces(args):
    from app.owner_deploy_events import SURFACE_HTTP_API, SURFACE_SHARED_TUNNEL
    return {
        SURFACE_HTTP_API: not args.spare_http_api,
        SURFACE_SHARED_TUNNEL: bool(args.tunnel_reinstall),
    }


def check_window(args):
    from app.deploy_window import evaluate

    moment = datetime.fromisoformat(args.at) if args.at else None
    decision = evaluate(moment)
    payload = decision.as_dict()
    payload['command'] = 'check-window'
    payload['forced'] = bool(args.force)
    payload['proceed'] = decision.allowed or bool(args.force)
    _print(payload)
    return 0 if payload['proceed'] else 3


def announce(args):
    from app.database import Database
    from app.owner_deploy_events import record

    database = Database()
    try:
        result = record(
            database,
            deploy_id=args.deploy_id,
            phase=args.phase,
            release_id=args.release_id,
            git_sha=args.git_sha,
            surfaces=_surfaces(args),
            note=args.note,
        )
    finally:
        database.close()
    payload = result.as_dict()
    payload['command'] = 'announce'
    payload['release_id'] = args.release_id
    payload['surfaces'] = _surfaces(args)
    _print(payload)
    return 0


def show_latest(args):
    from app.database import Database
    from app.owner_deploy_events import in_progress, latest

    database = Database()
    try:
        with database.transaction() as connection:
            items = latest(connection, limit=args.limit)
    finally:
        database.close()
    _print({'command': 'latest', 'items': items, 'in_progress': in_progress(items)})
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', default=DEFAULT_ENV_FILE)
    sub = parser.add_subparsers(dest='command', required=True)

    window = sub.add_parser('check-window', help='refuse the session and the peer batch window')
    window.add_argument('--at', help='ISO 8601 moment to judge instead of now')
    window.add_argument('--force', action='store_true', help='deliberate override; the reason is still printed')
    window.set_defaults(handler=check_window)

    event = sub.add_parser('announce', help='append one deploy phase')
    event.add_argument('--phase', required=True, choices=('starting', 'completed', 'failed'))
    event.add_argument('--deploy-id', required=True)
    event.add_argument('--release-id', required=True)
    event.add_argument('--git-sha')
    event.add_argument('--tunnel-reinstall', action='store_true',
                       help='this deploy will restart the shared-peer tunnel')
    event.add_argument('--spare-http-api', action='store_true',
                       help='this deploy will not restart the owner HTTP surface')
    event.add_argument('--note')
    event.set_defaults(handler=announce)

    read = sub.add_parser('latest', help='read the announcements back')
    read.add_argument('--limit', type=int, default=20)
    read.set_defaults(handler=show_latest)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    load_dotenv(args.env_file, override=True)
    try:
        return args.handler(args)
    except Exception as error:  # noqa: BLE001 - the receipt is the interface, including on failure
        _print({'command': args.command, 'status': 'failed',
                'error_type': type(error).__name__, 'error': str(error)[:300]})
        return 0 if args.command == 'announce' else 2


if __name__ == '__main__':
    raise SystemExit(main())
