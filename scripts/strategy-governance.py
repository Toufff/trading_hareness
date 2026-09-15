"""Local governance queue and human approval CLI. No HTTP mutation or code execution."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'quant-service'))


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def human_challenge(actor, challenge):
    from app.strategy_governance.rules import require
    require(actor.get('kind') == 'human' and actor.get('roles') == ['human'], 'Human-only action')
    require(sys.stdin.isatty() and sys.stdout.isatty(), 'Human activation requires an interactive terminal; no --yes bypass')
    print('此操作只切换已支持的策略配置，不进行交易。请确认你本人审阅了证据。')
    require(input('输入完整确认短语：' + challenge + '\n> ') == challenge, 'Human confirmation did not match')


def verify_measurement(payload):
    from app.strategy_governance.evidence import verify_measurement as verify
    verify(payload)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['create', 'get', 'transition', 'claim', 'active', 'activate', 'rollback'])
    p.add_argument('item_id', nargs='?')
    p.add_argument('--actor')
    p.add_argument('--registry')
    p.add_argument('--env-file', default='G:/StockPlatform/config/runtime.env')
    p.add_argument('--payload')
    p.add_argument('--revision', type=int)
    p.add_argument('--stage')
    p.add_argument('--role')
    p.add_argument('--artifact-hash')
    p.add_argument('--generation', type=int)
    p.add_argument('--target-generation', type=int)
    p.add_argument('--reason')
    args = p.parse_args()
    for line in Path(args.env_file).read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1); os.environ[key] = value
    from app.database import Database
    from app.strategy_governance import repository as repo
    from app.strategy_governance.identity import resolve_actor
    from app.strategy_governance.rules import GovernanceError
    db = Database()
    try:
        actor = resolve_actor(args.actor, args.registry) if args.action not in ('get', 'active') else None
        if args.action == 'create':
            result = repo.create_issue(db, read_json(args.payload), actor)
        elif args.action == 'get': result = repo.get_issue(db, args.item_id)
        elif args.action == 'active': result = repo.resolve_active_config(db)
        elif args.action == 'claim': result = repo.claim_work(db, actor, args.role)
        elif args.action == 'transition':
            payload = read_json(args.payload)
            if args.stage == 'observe': verify_measurement(payload)
            if args.stage == 'validate':
                item = repo.get_issue(db, args.item_id)
                verify_measurement(item['experiments'][item['current_experiment']]['evidence'])
            result = repo.transition(db, args.item_id, args.revision, args.stage, payload, actor)
        elif args.action == 'activate':
            item = repo.get_issue(db, args.item_id)
            print(json.dumps(item.get('ready'), ensure_ascii=False, indent=2))
            human_challenge(actor, f'ACTIVATE {args.item_id} {args.revision} {args.artifact_hash}')
            result = repo.activate(db, args.item_id, args.revision, args.artifact_hash, actor)
        else:
            human_challenge(actor, f'ROLLBACK {args.generation} TO {args.target_generation}')
            result = repo.rollback(db, args.target_generation, args.generation, actor, args.reason or '')
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    except (GovernanceError, TypeError, KeyError, OSError) as error:
        print(json.dumps({'status':'rejected','reason':str(error)}, ensure_ascii=False))
        raise SystemExit(2)
    finally:
        db.close()


if __name__ == '__main__':
    main()
