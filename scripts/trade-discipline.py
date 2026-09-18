"""Trade discipline CLI: generate / evaluate / reconcile / show.

Research-only.  Nothing here connects to a broker or submits an order: a plan is
a machine-derived, append-only statement of what the evidence implies, and a
human decides what to do with it.

Four commands, one JSON receipt on stdout each:

* ``generate``  freeze the evidence, derive one plan per symbol, write the
  discipline card, and (unless ``--dry-run``) persist the run and the plans and
  read them back;
* ``evaluate``  judge stored plans against a later session (daily or minute);
* ``reconcile`` judge real broker fills against what the plans signalled;
* ``show``      print one stored plan with its newest evaluation and compliance.

``--dry-run`` reads the database inside an explicitly read-only transaction and
writes only files, so it is safe against production.
"""
import argparse
import asyncio
import json
import sys
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'quant-service'))
from dotenv import load_dotenv  # noqa: E402

SHANGHAI = ZoneInfo('Asia/Shanghai')
SESSION_CLOSE = time(15, 0)
DEFAULT_OUTPUT_ROOT = Path('G:/StockPlatform/reports/discipline')
DEFAULT_ACCOUNT = 'citics-primary'
BOUNDARY = 'research_only_human_decision_support'
EVALUATION_BAR_COUNT = 90
UNPLANNED_TAIL_DAYS = 5


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def _shanghai(text, fallback):
    """Parse an ISO stamp as an exchange-local time; naive input means 'here'."""
    if not text:
        return fallback
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(SHANGHAI)


@contextmanager
def _read_only(db):
    """One transaction the database itself refuses to let write."""
    with db.transaction() as connection:
        connection.execute('SET TRANSACTION READ ONLY')
        yield connection


def _factory(db, dry_run):
    return (lambda: _read_only(db)) if dry_run else db.transaction


def _holdings(connection, account_key, as_of, inputs_module):
    snapshot = inputs_module.latest_broker_snapshot(connection, account_key, as_of)
    if snapshot is None:
        return []
    return [row['symbol'] for row in inputs_module.broker_positions(connection, snapshot['snapshot_id'])
            if int(row.get('quantity') or 0) > 0]


def _sector_change(connection, plan, day, inputs_module):
    sector = (plan.metrics or {}).get('sector') or {}
    if not sector.get('code'):
        return None, 'no_stored_sector_membership'
    change = inputs_module.sector_daily_change(connection, sector.get('taxonomy') or 'longhu_ths_industry',
                                               sector['code'], day)
    return change, ('sector_flow_row_missing' if change is None else f"sector:{sector['code']}")


def _minute_tape(symbol, day):
    """Today's licensed minute tape, or an empty tape with the reason why."""
    from app.longhu_market_data import longhu_intraday_minute_session
    try:
        session = asyncio.run(longhu_intraday_minute_session(symbol))
    except Exception as error:  # noqa: BLE001 - an absent tape degrades, never guesses
        return [], f'minute_tape_unavailable:{type(error).__name__}'
    if session.get('session_date') != day.isoformat():
        return [], f"minute_tape_is_for_{session.get('session_date')}"
    return session['rows'], f"minute_tape:{len(session['rows'])}_rows"


def _trade_rows(connection, account_key, symbol, start, end):
    rows = connection.execute("""
        SELECT record_id,trade_date,trade_time,symbol,name,side,quantity,price
          FROM quant.broker_trade_records
         WHERE account_key=%s AND symbol=%s AND trade_date BETWEEN %s AND %s
         ORDER BY trade_date,trade_time,record_id""", (account_key, symbol, start, end)).fetchall()
    return [{'trade_record_id': str(row['record_id']), 'trade_date': row['trade_date'],
             'trade_time': row['trade_time'], 'symbol': row['symbol'], 'name': row['name'] or '',
             'side': row['side'], 'quantity': int(row['quantity']),
             'price': Decimal(str(row['price'] or 0))} for row in rows if row['price']]


# --------------------------------------------------------------------------
# generate
# --------------------------------------------------------------------------
def command_generate(args, db):
    from app.trade_discipline import inputs as inputs_module
    from app.trade_discipline import repository
    from app.trade_discipline.generator import GENERATOR_VERSION, generate
    from app.trade_discipline.report import write_report

    as_of = _shanghai(args.as_of, datetime.now(SHANGHAI))
    run_id = args.run_id or str(uuid.uuid4())
    factory = _factory(db, args.dry_run)
    symbols = list(dict.fromkeys(args.symbol or []))
    if not symbols:
        with _read_only(db) as connection:   # resolving the holdings is always a read
            symbols = _holdings(connection, args.account_key, as_of, inputs_module)

    plans, frozen, receipts, errors = [], {}, [], []
    for symbol in symbols:
        try:
            generation_inputs = asyncio.run(inputs_module.collect(
                factory, run_id=run_id, account_key=args.account_key, symbol=symbol, as_of=as_of,
                risk_per_trade_pct=Decimal(str(args.risk_per_trade_pct)),
                lowered_reason=args.lowered_reason, allow_live=not args.no_live))
            plan = generate(generation_inputs)
        except Exception as error:  # noqa: BLE001 - one bad symbol must not void the run
            errors.append({'symbol': symbol, 'error': f'{type(error).__name__}: {str(error)[:300]}'})
            continue
        frozen[symbol] = generation_inputs.model_dump(mode='json')
        plans.append(plan)
        written = write_report(plan, output_root=args.output_dir)
        hard_stop = plan.sizing.hard_stop if plan.sizing else None
        receipts.append({
            'symbol': symbol, 'name': plan.name, 'stage': plan.stage, 'plan_kind': plan.plan_kind,
            'status': plan.status, 'plan_key': plan.plan_key,
            'quality_failed': [check.check_id for check in plan.quality if not check.passed],
            'hard_stop': hard_stop, 'reference_price': plan.sizing.reference_price if plan.sizing else None,
            'recommended_shares': plan.sizing.recommended_shares if plan.sizing else None,
            'lines': len(plan.lines), 'valid_until': plan.valid_until.isoformat(),
            'inputs_hash': plan.inputs_hash, 'evidence_refs': plan.evidence_refs,
            'plan_id': None, 'persisted': 'skipped_dry_run', **written,
        })

    if not args.dry_run and plans:
        run_payload = {'account_key': args.account_key, 'as_of': as_of.isoformat(),
                       'symbols': symbols, 'inputs': frozen}
        with db.transaction() as connection:
            repository.persist_generation_run(
                connection, run_id=run_id, account_key=args.account_key, as_of_at=as_of,
                trading_date=plans[0].trading_date, generator_version=GENERATOR_VERSION,
                inputs_hash=inputs_module.inputs_hash(run_payload), inputs=run_payload, status='generated')
            for plan, receipt in zip(plans, receipts):
                try:
                    stored = repository.persist_plan(connection, plan, run_id=run_id)
                except repository.DisciplineFactConflict as conflict:
                    receipt['persisted'] = 'conflict'
                    errors.append({'symbol': plan.symbol, 'error': f'DisciplineFactConflict: {conflict}'})
                    continue
                readback = stored.get('plan') or {}
                receipt.update({'plan_id': stored['plan_id'], 'persisted': stored['status'],
                                'content_hash': stored['content_hash'],
                                'readback_ok': bool(readback) and readback.get('plan_key') == plan.plan_key
                                and str(readback.get('content_hash')) == stored['content_hash']
                                and readback.get('status') == plan.status})
                if plan.supersedes_plan_id and plan.status == 'active':
                    receipt['superseded_previous'] = repository.mark_superseded(
                        connection, plan.supersedes_plan_id, by_plan_id=stored['plan_id'])

    return {'command': 'generate', 'dry_run': bool(args.dry_run), 'run_id': run_id,
            'account_key': args.account_key, 'as_of': as_of.isoformat(),
            'output_root': str(args.output_dir), 'symbols': symbols,
            'generated': len(receipts), 'plans': receipts, 'errors': errors,
            'live_orders': False, 'boundary': BOUNDARY}


# --------------------------------------------------------------------------
# evaluate
# --------------------------------------------------------------------------
def _stored_plans(connection, args, repository, statuses=('active',)):
    """The stored plans this invocation judges: one by id, or the newest per symbol."""
    if args.plan_id:
        row = repository.read_plan(connection, args.plan_id)
        rows = [row] if row else []
    else:
        rows = repository.latest_plans(connection, args.account_key, statuses=statuses, limit=200)
    wanted = set(args.symbol or [])
    return [row for row in rows if not wanted or row['symbol'] in wanted]


def command_evaluate(args, db):
    from app.trade_discipline import inputs as inputs_module
    from app.trade_discipline import repository
    from app.trade_discipline.evaluator import EvaluationInputs, evaluate
    from app.trade_discipline.report import write_report

    day = date.fromisoformat(args.date) if args.date else datetime.now(SHANGHAI).date()
    default_as_of = (datetime.now(SHANGHAI) if args.basis == 'minute' and day == datetime.now(SHANGHAI).date()
                     else datetime.combine(day, SESSION_CLOSE, tzinfo=SHANGHAI))
    as_of = _shanghai(args.as_of, default_as_of)

    evaluations, receipts, errors = [], [], []
    with _read_only(db) as connection:
        rows = _stored_plans(connection, args, repository)
        for row in rows:
            plan_id = str(row['plan_id'])
            try:
                plan = repository.plan_from_row(row)
                bars = inputs_module.settled_daily_bars(connection, plan.symbol, day + timedelta(days=1),
                                                        EVALUATION_BAR_COUNT)
                calendar, _ = inputs_module.trading_calendar(connection, plan.trading_date)
                change, sector_note = _sector_change(connection, plan, day, inputs_module)
                if args.basis != 'minute':
                    minutes, tape_note = [], 'daily_basis'
                elif day != datetime.now(SHANGHAI).date():
                    # The licensed endpoint serves only its latest session, so a
                    # past minute evaluation fails closed instead of guessing.
                    minutes, tape_note = [], 'minute_tape_only_available_for_today'
                else:
                    minutes, tape_note = _minute_tape(plan.symbol, day)
                payload = EvaluationInputs(
                    plan_id=plan_id, as_of=as_of, basis=args.basis, bars=bars, minutes=minutes,
                    calendar=calendar, sector_change_pct=change,
                    evidence_refs=[f'bars:{len(bars)}:{day.isoformat()}', sector_note, tape_note])
                evaluation = evaluate(plan, payload)
            except Exception as error:  # noqa: BLE001
                errors.append({'plan_id': plan_id, 'error': f'{type(error).__name__}: {str(error)[:300]}'})
                continue
            evaluations.append((plan, evaluation))
            receipts.append({
                'plan_id': plan_id, 'symbol': plan.symbol, 'name': plan.name, 'stage': plan.stage,
                'basis': evaluation.basis, 'as_of': evaluation.as_of_at.isoformat(),
                'plan_state': evaluation.plan_state, 'bars': len(payload.bars), 'minutes': len(payload.minutes),
                'triggered': [state.kind for state in evaluation.line_states if state.state == 'triggered'],
                'armed': [state.kind for state in evaluation.line_states if state.state == 'armed'],
                'inputs_hash': evaluation.inputs_hash, 'persisted': 'skipped_dry_run',
            })

    if not args.dry_run and evaluations:
        with db.transaction() as connection:
            for receipt, (_, evaluation) in zip(receipts, evaluations):
                try:
                    stored = repository.persist_evaluation(connection, evaluation)
                except repository.DisciplineFactConflict as conflict:
                    receipt['persisted'] = 'conflict'
                    errors.append({'plan_id': evaluation.plan_id, 'error': f'DisciplineFactConflict: {conflict}'})
                    continue
                receipt.update({'persisted': stored['status'], 'evaluation_id': stored['evaluation_id']})

    for receipt, (plan, evaluation) in zip(receipts, evaluations):
        receipt.update(write_report(plan, output_root=args.output_dir, evaluation=evaluation,
                                    plan_id=receipt['plan_id']))

    return {'command': 'evaluate', 'dry_run': bool(args.dry_run), 'account_key': args.account_key,
            'date': day.isoformat(), 'basis': args.basis, 'as_of': as_of.isoformat(),
            'output_root': str(args.output_dir), 'evaluated': len(receipts), 'evaluations': receipts,
            'errors': errors, 'live_orders': False, 'boundary': BOUNDARY}


# --------------------------------------------------------------------------
# reconcile
# --------------------------------------------------------------------------
def command_reconcile(args, db):
    from app.trade_discipline import inputs as inputs_module
    from app.trade_discipline import repository
    from app.trade_discipline.contracts import Evaluation
    from app.trade_discipline.reconcile import reconcile
    from app.trade_discipline.report import write_report

    day = date.fromisoformat(args.date) if args.date else datetime.now(SHANGHAI).date()
    as_of = _shanghai(args.as_of, datetime.combine(day, SESSION_CLOSE, tzinfo=SHANGHAI))

    judged, receipts, errors = [], [], []
    with _read_only(db) as connection:
        for row in _stored_plans(connection, args, repository):
            plan_id = str(row['plan_id'])
            try:
                plan = repository.plan_from_row(row)
                evaluations = [Evaluation(plan_id=plan_id, as_of_at=item['as_of_at'],
                                          trading_date=item['trading_date'], basis=item['basis'],
                                          line_states=item['line_states'], plan_state=item['plan_state'],
                                          inputs_hash=item['inputs_hash'])
                               for item in repository.plan_evaluations(connection, plan_id)]
                calendar, _ = inputs_module.trading_calendar(connection, plan.trading_date)
                trades = _trade_rows(connection, args.account_key, plan.symbol, plan.trading_date,
                                     plan.valid_until.astimezone(SHANGHAI).date()
                                     + timedelta(days=UNPLANNED_TAIL_DAYS))
                records = reconcile(plan, evaluations, trades, as_of=as_of, calendar=calendar, plan_id=plan_id)
            except Exception as error:  # noqa: BLE001
                errors.append({'plan_id': plan_id, 'error': f'{type(error).__name__}: {str(error)[:300]}'})
                continue
            judged.append((plan, records))
            verdicts = {}
            for record in records:
                verdicts[record.verdict] = verdicts.get(record.verdict, 0) + 1
            receipts.append({'plan_id': plan_id, 'symbol': plan.symbol, 'name': plan.name,
                             'evaluations': len(evaluations), 'trades': len(trades),
                             'records': len(records), 'verdicts': verdicts,
                             'details': [{'verdict': record.verdict, 'line_kind': record.line_kind,
                                          'trade_record_id': record.trade_record_id, 'notes': record.notes}
                                         for record in records],
                             'persisted': 'skipped_dry_run'})

    if not args.dry_run and judged:
        with db.transaction() as connection:
            for receipt, (_, records) in zip(receipts, judged):
                stored = [repository.persist_compliance(connection, record) for record in records]
                receipt.update({'persisted': 'written',
                                'compliance_ids': [item['compliance_id'] for item in stored]})

    for receipt, (plan, records) in zip(receipts, judged):
        receipt.update(write_report(plan, output_root=args.output_dir, compliance=records,
                                    plan_id=receipt['plan_id']))

    return {'command': 'reconcile', 'dry_run': bool(args.dry_run), 'account_key': args.account_key,
            'date': day.isoformat(), 'as_of': as_of.isoformat(), 'output_root': str(args.output_dir),
            'reconciled': len(receipts), 'plans': receipts, 'errors': errors,
            'live_orders': False, 'boundary': BOUNDARY}


# --------------------------------------------------------------------------
# show
# --------------------------------------------------------------------------
def command_show(args, db):
    from app.trade_discipline import repository
    from app.trade_discipline.contracts import ComplianceRecord, Evaluation
    from app.trade_discipline.report import plan_payload, report_paths

    with _read_only(db) as connection:
        rows = _stored_plans(connection, args, repository, statuses=repository.ACTIVE_STATUSES)
        if not rows:
            return {'command': 'show', 'account_key': args.account_key, 'symbol': args.symbol,
                    'found': 0, 'plans': [], 'live_orders': False, 'boundary': BOUNDARY}
        cards = []
        for row in rows:
            plan_id = str(row['plan_id'])
            plan = repository.plan_from_row(row)
            latest = repository.latest_evaluation(connection, plan_id, basis=args.basis)
            evaluation = None if latest is None else Evaluation(
                plan_id=plan_id, as_of_at=latest['as_of_at'], trading_date=latest['trading_date'],
                basis=latest['basis'], line_states=latest['line_states'], plan_state=latest['plan_state'],
                inputs_hash=latest['inputs_hash'])
            compliance = [ComplianceRecord(plan_id=plan_id, trade_record_id=item['trade_record_id'],
                                           line_kind=item['line_kind'], verdict=item['verdict'],
                                           deviation=item['deviation'], notes=item['notes'])
                          for item in repository.plan_compliance(connection, plan_id)]
            paths = report_paths(args.output_dir, plan)
            cards.append({**plan_payload(plan, evaluation=evaluation, compliance=compliance, plan_id=plan_id),
                          'markdown_path': str(paths['markdown']), 'json_path': str(paths['json'])})
    return {'command': 'show', 'account_key': args.account_key, 'symbol': args.symbol,
            'found': len(cards), 'plans': cards, 'live_orders': False, 'boundary': BOUNDARY}


COMMANDS = {'generate': command_generate, 'evaluate': command_evaluate,
            'reconcile': command_reconcile, 'show': command_show}


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--account-key', default=DEFAULT_ACCOUNT)
    common.add_argument('--symbol', action='append', help='repeatable, e.g. --symbol 600613.SH')
    common.add_argument('--env-file', default='G:/StockPlatform/config/runtime.env')
    common.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT_ROOT,
                        help='report root; default G:/StockPlatform/reports/discipline')
    common.add_argument('--dry-run', action='store_true',
                        help='read the database read-only and write only report files')

    parser = argparse.ArgumentParser(
        prog='trade-discipline',
        description='机器推导的交易纪律：生成 / 评估 / 对账 / 查看（研究用途，不下单）')
    sub = parser.add_subparsers(dest='command', required=True)

    generate = sub.add_parser('generate', parents=[common], help='从证据推导纪律计划并生成纪律卡')
    generate.add_argument('--as-of', help='ISO time in Asia/Shanghai; default now')
    generate.add_argument('--run-id', help='reuse one generation run id (uuid)')
    generate.add_argument('--risk-per-trade-pct', default='1.0')
    generate.add_argument('--lowered-reason', help='required when the hard stop falls below the previous plan')
    generate.add_argument('--no-live', action='store_true', help='settled bars only, never a live quote')

    evaluate = sub.add_parser('evaluate', parents=[common], help='用行情评估已落库的计划')
    evaluate.add_argument('--date', help='YYYY-MM-DD; default today')
    evaluate.add_argument('--basis', choices=['daily', 'minute'], default='daily')
    evaluate.add_argument('--as-of', help='ISO time in Asia/Shanghai; default the session close')
    evaluate.add_argument('--plan-id', help='evaluate exactly one stored plan')

    reconcile = sub.add_parser('reconcile', parents=[common], help='把真实成交与计划信号对账')
    reconcile.add_argument('--date', help='YYYY-MM-DD; default today')
    reconcile.add_argument('--as-of', help='ISO time in Asia/Shanghai; default the session close')
    reconcile.add_argument('--plan-id', help='reconcile exactly one stored plan')

    show = sub.add_parser('show', parents=[common], help='打印已落库的纪律卡')
    show.add_argument('--plan-id')
    show.add_argument('--basis', choices=['daily', 'minute'])
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    load_dotenv(args.env_file, override=True)

    from app.database import Database

    db = Database()
    try:
        receipt = COMMANDS[args.command](args, db)
        print(_json(receipt))
        return 0 if not receipt.get('errors') else 1
    except Exception as error:  # noqa: BLE001 - the receipt is the interface, including on failure
        print(_json({'command': args.command, 'status': 'failed', 'error_type': type(error).__name__,
                     'error': str(error)[:300], 'live_orders': False, 'boundary': BOUNDARY}))
        return 2
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
