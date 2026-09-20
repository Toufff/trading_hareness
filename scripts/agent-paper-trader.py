"""Agent paper trader: initialize, run one trading day, decide once, or report.

Paper-only research.  It never touches a broker, and the model only receives
JSON context through a configured local model transport.
"""
import argparse
import asyncio
import json
import msvcrt
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'quant-service'))
from dotenv import load_dotenv  # noqa: E402

SHANGHAI = ZoneInfo('Asia/Shanghai')


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['init', 'run-day', 'decide-once', 'report', 'model-check'])
    parser.add_argument('--env-file', default='G:/StockPlatform/config/runtime.env')
    parser.add_argument('--platform-root', type=Path, default=Path('G:/StockPlatform'))
    parser.add_argument('--account-key', default='agent-claude-opus')
    parser.add_argument('--source-account', default='citics-primary')
    parser.add_argument('--start-at', help='ISO time in Asia/Shanghai, e.g. 2026-09-17T13:00')
    parser.add_argument('--model')
    parser.add_argument('--backend', choices=['claude_cli', 'codex_cli', 'event_research', 'dsh'])
    parser.add_argument('--reasoning-effort')
    parser.add_argument('--decision-minutes', type=int, default=5)
    parser.add_argument('--day')
    args = parser.parse_args()
    load_dotenv(args.env_file, override=True)

    from app.database import Database
    from app.event_research.trading_calendar import is_open
    from app.agent_paper.model import build_model
    from app.agent_paper.report import status
    from app.agent_paper.runner import Runner, initialize_account, run_day

    log_path = args.platform_root / 'logs' / f'agent-paper-{args.account_key}.jsonl'
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(entry):
        entry = {'recorded_at': datetime.now(timezone.utc).isoformat(), **entry}
        with log_path.open('a', encoding='utf-8') as stream:
            stream.write(_json(entry) + '\n')

    db = Database()
    try:
        if args.command == 'init':
            model = build_model(args.backend, model=args.model, reasoning_effort=args.reasoning_effort)
            start = datetime.fromisoformat(args.start_at).replace(tzinfo=SHANGHAI) if args.start_at else datetime.now(SHANGHAI)
            result = initialize_account(db, account_key=args.account_key, model=model.model,
                                        source_account=args.source_account, start_at=start)
            log({'event': 'init', **result})
            print(_json(result))
            return 0
        if args.command == 'model-check':
            # One tiny call proving the CLI login works in this environment; no ledger change.
            from app.agent_paper.model import ModelFailure
            model = build_model(args.backend, model=args.model, reasoning_effort=args.reasoning_effort)
            try:
                result = model.decide(_json({'now': datetime.now(SHANGHAI).isoformat(), 'check': '连通性检查：不要下单，orders 返回空数组'}))
                entry = {'event': 'model_check', 'status': 'ok', 'model': result.model, 'duration_ms': result.duration_ms,
                         'orders': result.output.get('orders'), 'usage': result.usage,
                         'cost_usd': (result.usage or {}).get('total_cost_usd')}
            except ModelFailure as failure:
                entry = {'event': 'model_check', 'status': 'failed', 'model': model.model, 'error': failure.code, 'detail': failure.detail}
            log(entry)
            print(_json(entry))
            return 0 if entry['status'] == 'ok' else 3
        if args.command == 'report':
            with db.transaction() as connection:
                day = date.fromisoformat(args.day) if args.day else None
                print(_json(status(connection, account_key=args.account_key, day=day)))
            return 0
        now = datetime.now(SHANGHAI)
        if not is_open(now.date()):
            log({'event': 'closed_day', 'day': now.date().isoformat()})
            print(_json({'status': 'closed_day'}))
            return 0
        with db.transaction() as connection:
            account_row = connection.execute('SELECT baseline FROM quant.agent_paper_accounts WHERE account_key=%s', (args.account_key,)).fetchone()
        if account_row is None:
            raise SystemExit(f'account {args.account_key} is not initialized')
        start_at = datetime.fromisoformat(account_row['baseline'].get('start_at') or '2000-01-01T00:00:00+08:00')
        runner = Runner(db, args.account_key, build_model(args.backend, model=args.model,
                                                          reasoning_effort=args.reasoning_effort),
                        decision_minutes=args.decision_minutes, start_at=start_at)
        if args.command == 'decide-once':
            runner.start_of_day(now)
            outcome = asyncio.run(runner.decide(now))
            log({'event': 'manual_decision', **outcome})
            print(_json(outcome))
            return 0
        lock_path = args.platform_root / 'run' / f'agent-paper-{args.account_key}.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, 'a+')
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            print(_json({'status': 'already_running'}))
            return 0
        log({'event': 'day_start', 'at': now.isoformat()})
        summary = asyncio.run(run_day(runner, now_fn=lambda: datetime.now(SHANGHAI), log=log))
        print(_json(summary))
        return 0
    except SystemExit:
        raise
    except Exception as error:
        log({'event': 'failed', 'command': args.command, 'error': f'{type(error).__name__}: {str(error)[:300]}'})
        print(_json({'status': 'failed', 'error_type': type(error).__name__, 'error': str(error)[:300]}))
        return 2
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
