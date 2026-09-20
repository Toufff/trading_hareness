"""Stored scan -> immutable research hypotheses -> bounded shadow evaluations.

No rescoring, provider calls, order operations or new implicit execution rules.
Legacy prose stays prose. Raw prices are compared with explicit original lines,
not silently promoted into the complete entry/invalidation condition.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from math import isfinite
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from .evidence import parse_time
from .repository import list_latest
from .service import capture_evaluate

VERSION = 'thesis-scan-adapter-20260920.1'
SH = ZoneInfo('Asia/Shanghai')
MAX_SYMBOLS = 500
# quant.canonical_bars_daily follows the Tushare-compatible persistence
# contract: daily ``amount`` is thousand CNY.  Thesis evidence and presentation
# use CNY, matching money-flow and intraday quote amounts.
CANONICAL_DAILY_AMOUNT_CNY_MULTIPLIER = 1000


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def number(value):
    try:
        n = float(value)
        return n if isfinite(n) and not isinstance(value, bool) else None
    except (ValueError, TypeError):
        return None


def scan_candidates(scan):
    """All displayed candidates, never only rank one and never artificial Top10."""
    by_symbol = {}
    for lane in scan.get('lanes', []):
        key = lane['key']
        items = lane.get('items') if 'items' in lane else lane.get('selected', []) + lane.get('observation_list', [])
        for index, item in enumerate(items or [], 1):
            symbol = item.get('symbol')
            if not symbol:
                continue
            row = by_symbol.setdefault(symbol, {'symbol': symbol, 'name': item.get('name') or symbol,
                                                'memberships': [], 'items': {}})
            if key in row['items']:
                continue
            rank = item.get('formal_rank') or item.get('rank') or index
            row['memberships'].append({'lane': key, 'rank': rank,
                'population': lane.get('total_matches'), 'score': item.get('rank_score'),
                'origin_id': item.get('origin_id') or digest([symbol, key, scan.get('as_of_date') or scan.get('cutoff')])})
            row['items'][key] = item
    for row in by_symbol.values():
        row['memberships'].sort(key=lambda m: (m['lane'], m['rank'], m['origin_id']))
    return by_symbol


def observable_condition(key, metric, operator, threshold, benchmark):
    return dict(condition_id=key, purpose='structure_support', metric=metric, operator=operator,
        threshold=threshold, unit='CNY', basis='settled_daily', window='current', benchmark=benchmark,
        confirmation='single', evidence_requirement='exact_basis_unit_benchmark', derivation='identity',
        severity='soft', version=VERSION)


def seed_thesis(row, source_run_id, source_available_at, captured_at, deadline, origin_mode='reconstructed'):
    from ..strategy_origin import select_primary_origin
    primary = select_primary_origin(row)
    lane = primary['lane']
    item = row['items'][lane]
    metrics = item.get('metrics') or {}
    support = number(item.get('support') or metrics.get('recent_low') or metrics.get('low5'))
    reference = number(item.get('reference') or metrics.get('prior_high') or metrics.get('prior_high5'))
    invariants = [] if support is None else [observable_condition('original-support-close', 'close', 'gte', support, 'original_support')]
    confirmations = [] if reference is None else [observable_condition('original-platform-close', 'close', 'gt', reference, 'original_reference')]
    # Legacy phrases such as "effective breakout" lack duration/sector contracts.
    # Keep entry unknown instead of inventing an executable price-only signal.
    entry = observable_condition('full-entry-scenario', 'full_entry_scenario_confirmed', 'eq', True, 'original_entry_scenario')
    entry.update(purpose='entry', unit='bool', basis='scenario', severity='required')
    return dict(thesis_id='thesis-' + digest([row['symbol'], lane, source_run_id])[:24], revision=1,
        symbol=row['symbol'], name=row['name'], family=lane, origin_primary_lane=lane,
        primary_origin_id=primary['origin_id'], origin_ids=[m['origin_id'] for m in row['memberships']],
        origin_selector=primary, original_rankings=row['memberships'],
        source_run_id=source_run_id, source_available_at=source_available_at,
        created_at=captured_at, available_at=captured_at, effective_from=captured_at,
        terminal_deadline=deadline, review_due_at=deadline,
        origin_mode=origin_mode, eligibility_policy_version=VERSION,
        claim=item.get('original_reason') or item.get('reason') or item.get('current_reason') or '既有策略发现；保留原始证据',
        original_confirmation=item.get('original_confirmation') or item.get('confirmation') or item.get('entry_scenario'),
        original_invalidation=item.get('original_invalidation') or item.get('invalidation'),
        original_structure={'support': support, 'reference': reference, 'basis': 'original_scan_reference_not_stop'},
        original_metrics={k: metrics.get(k) for k in ('close', 'amount', 'net5', 'amount_multiple', 'change_pct')},
        invariants=invariants, confirmation_scenarios=confirmations, invalidation_conditions=[], entry_conditions=[entry],
        unstructured_conditions=True, live_effect='none',
        interpretation='价格线仅验证原结构的一部分；未把自由文本中的承接、板块与时长条件简化成买卖信号。')


def market_evidence(symbol, bars, flows, cutoff_at, expected_date, expected_days=None):
    """Filter before computing ratios; do not bridge missing exchange sessions."""
    cutoff = parse_time(cutoff_at, 'cutoff_at')
    rows = sorted([b for b in bars if b.get('available_at') and parse_time(str(b['available_at']), 'available_at') <= cutoff
                   and str(b['trading_date']) <= expected_date], key=lambda r: str(r['trading_date']))
    evidence = []
    if not rows:
        return evidence
    latest = rows[-1]
    if str(latest['trading_date']) != expected_date:
        return evidence  # stale is not the current price
    actual_days = [str(b['trading_date']) for b in rows]
    complete = (expected_days is not None and actual_days == expected_days
                and len(actual_days) == len(set(actual_days)))
    def ref(b):
        return dict(evidence_id='bar:' + digest([symbol, str(b['trading_date']), b.get('available_at'),
                    b.get('close'), b.get('amount'), b.get('source_observation_ids')]),
                    available_at=str(b['available_at']))
    def emit(metric, value, unit, benchmark, dependencies=None):
        if value is None:
            return
        deps = dependencies or [ref(latest)]
        available = max(parse_time(d['available_at'], 'available_at') for d in deps).isoformat()
        item = dict(evidence_id='metric:' + digest([symbol, metric, benchmark, value, deps]),
            symbol=symbol, metric=metric, value=value, unit=unit, basis='settled_daily', benchmark=benchmark,
            effective_at=datetime.combine(date.fromisoformat(expected_date), time(15), SH).isoformat(),
            available_at=available, availability_basis='stored_available_at', version=VERSION)
        if dependencies:
            item.update(derivation='bounded_available_inputs', dependencies=deps)
        evidence.append(item)
    for benchmark in ('original_support', 'original_reference'):
        emit('close', number(latest.get('close')), 'CNY', benchmark)
    canonical_amount = number(latest.get('amount'))
    emit('amount', None if canonical_amount is None else canonical_amount * CANONICAL_DAILY_AMOUNT_CNY_MULTIPLIER,
         'CNY', 'current_settled_session')
    emit('low', number(latest.get('low')), 'CNY', 'current_settled_session')
    amount = number(latest.get('amount'))
    if complete and len(rows) >= 2 and amount is not None and number(rows[-2].get('amount')):
        emit('amount_ratio_previous', amount / float(rows[-2]['amount']), 'ratio', 'previous_session', [ref(b) for b in rows[-2:]])
    if complete and len(rows) >= 6 and amount is not None and all(number(b.get('amount')) is not None for b in rows[-6:-1]):
        mean = sum(float(b['amount']) for b in rows[-6:-1]) / 5
        if mean > 0:
            emit('amount_ratio_mean5', amount / mean, 'ratio', 'previous_5_sessions_mean', [ref(b) for b in rows[-6:]])
    eligible_flow = {str(f['trading_date']): f for f in flows if f.get('available_at') and
        parse_time(str(f['available_at']), 'available_at') <= cutoff and number(f.get('net_amount')) is not None}
    days = [str(b['trading_date']) for b in rows[-5:]]
    if complete and len(days) == 5 and all(day in eligible_flow for day in days):
        selected = [eligible_flow[d] for d in days]
        deps = [dict(evidence_id='flow:' + digest([symbol, str(f['trading_date']), f['net_amount'], str(f['available_at'])]),
                     available_at=str(f['available_at'])) for f in selected]
        emit('main_net5', sum(float(f['net_amount']) for f in selected), 'CNY', 'longhuvip_main_net_5_sessions', deps)
    return evidence


def intraday_evidence(symbol, item, cutoff_at, observed_at):
    """Project a stored intraday observation without pretending it is settled.

    ``observed_at`` must be the persisted scan observation timestamp.  An
    origin candidate's older ``available_at`` is deliberately not reused for
    the current quote.  Partial-session amount is an absolute observation only;
    it never produces a daily amount ratio.
    """
    if not item or not observed_at:
        return []
    cutoff = parse_time(cutoff_at, 'cutoff_at')
    available = parse_time(str(observed_at), 'intraday_observed_at')
    if available > cutoff:
        return []
    effective_at = item.get('cutoff_at') or item.get('cutoff')
    if effective_at and parse_time(str(effective_at), 'intraday_effective_at') > cutoff:
        return []
    source = item.get('source')
    minute_end = item.get('minute_end')
    fields = (
        ('price', item.get('price'), 'CNY', 'intraday_quote'),
        ('amount_so_far', item.get('amount'), 'CNY', 'intraday_cumulative_amount'),
        ('session_low_so_far', item.get('low'), 'CNY', 'intraday_session_low'),
        ('vwap', item.get('vwap'), 'CNY', 'intraday_vwap'),
    )
    result = []
    for metric, raw, unit, benchmark in fields:
        value = number(raw)
        if value is None:
            continue
        result.append(dict(
            evidence_id='intraday:' + digest([symbol, metric, value, observed_at, effective_at, minute_end, source]),
            symbol=symbol, metric=metric, value=value, unit=unit, basis='forming_intraday',
            benchmark=benchmark, effective_at=str(effective_at or observed_at), available_at=available.isoformat(),
            availability_basis='stored_intraday_scan_observed_at', version=VERSION,
            source=source, minute_end=minute_end,
        ))
    if isinstance(minute_end, str) and len(minute_end) == 4 and minute_end.isdigit():
        result.append(dict(
            evidence_id='intraday:' + digest([symbol, 'minute_end', minute_end, observed_at, effective_at, source]),
            symbol=symbol, metric='minute_end', value=minute_end, unit='HHMM', basis='forming_intraday',
            benchmark='intraday_minute_end', effective_at=str(effective_at or observed_at),
            available_at=available.isoformat(), availability_basis='stored_intraday_scan_observed_at',
            version=VERSION, source=source, minute_end=minute_end,
        ))
    return result


def load_source(database, source_run_id=None):
    with database.transaction() as c:
        c.execute('SET TRANSACTION READ ONLY')
        if source_run_id:
            UUID(str(source_run_id))
            row = c.execute('SELECT run_id,as_of_date,updated_at,summary,status FROM quant.post_close_strategy_runs WHERE run_id=%s', (source_run_id,)).fetchone()
        else:
            row = c.execute("SELECT run_id,as_of_date,updated_at,summary,status FROM quant.post_close_strategy_runs WHERE status='completed' AND summary ? 'strategy_lanes' ORDER BY as_of_date DESC,updated_at DESC LIMIT 1").fetchone()
        if row:
            if row['status'] not in ('completed', 'partial'):
                raise ValueError('source_scan_not_completed')
            return dict(kind='post_close', run_id=str(row['run_id']), data_date=str(row['as_of_date']),
                available_at=row['updated_at'].isoformat(), scan=row['summary'].get('strategy_lanes') or {})
        row = c.execute("SELECT run_id,cutoff,updated_at,result FROM quant.intraday_strategy_scans WHERE run_id=%s AND state='completed'", (source_run_id,)).fetchone()
        if not row:
            raise ValueError('completed_source_scan_not_found')
        return dict(kind='intraday', run_id=str(row['run_id']), data_date=str(row['cutoff'].astimezone(SH).date()),
                    available_at=row['updated_at'].isoformat(), scan=row['result'])


def evaluate_source_run(database, source_run_id=None, cutoff_at=None, symbols=None, namespace='shadow'):
    from .rules import evaluate_thesis
    source = load_source(database, source_run_id)
    now = datetime.now(timezone.utc)
    cutoff = parse_time(str(cutoff_at), 'cutoff_at') if cutoff_at else now
    if cutoff > now or parse_time(source['available_at'], 'source_available_at') > cutoff:
        raise ValueError('source_not_available_at_cutoff')
    if namespace not in ('capture', 'shadow', 'advisory'):
        raise ValueError('decision_binding_disabled')
    candidates = scan_candidates(source['scan'])
    with database.transaction() as c:
        c.execute('SET TRANSACTION READ ONLY')
        existing = list_latest(c, limit=MAX_SYMBOLS, namespace=namespace)
        next_row = c.execute("SELECT calendar_date FROM quant.market_trade_calendar WHERE exchange='SSE' AND is_open AND calendar_date>%s ORDER BY calendar_date LIMIT 1", (source['data_date'],)).fetchone()
        session_end = cutoff.astimezone(SH).date() - (timedelta(days=1) if cutoff.astimezone(SH).time() < time(15) else timedelta())
        sessions = c.execute("SELECT calendar_date FROM quant.market_trade_calendar WHERE exchange='SSE' AND is_open AND calendar_date<=%s ORDER BY calendar_date DESC LIMIT 6", (session_end,)).fetchall()
    if not next_row or not sessions:
        raise ValueError('verified_exchange_calendar_required')
    expected_days = [str(r['calendar_date']) for r in reversed(sessions)]
    expected_date = expected_days[-1]
    deadline = datetime.combine(next_row['calendar_date'], time(15), SH).isoformat()
    by_symbol = {}
    for row in existing:
        thesis = row['thesis']
        by_symbol.setdefault(thesis['symbol'], thesis)
    targets = sorted(set(candidates) | set(by_symbol))
    if symbols is not None:
        targets = sorted(set(targets) & set(symbols))
        if not targets:
            raise ValueError('no_matching_source_or_tracked_symbols')
    if len(targets) > MAX_SYMBOLS:
        raise ValueError('thesis_scope_exceeds_500_page_required')
    bars_by, flows_by = {}, {}
    with database.transaction() as c:
        c.execute('SET TRANSACTION READ ONLY')
        bars = c.execute('''SELECT symbol,trading_date,close,low,amount,available_at,source_observation_ids
            FROM quant.canonical_bars_daily WHERE symbol=ANY(%s) AND trading_date=ANY(%s)
              AND available_at<=%s ORDER BY symbol,trading_date''', (targets, [date.fromisoformat(d) for d in expected_days], cutoff)).fetchall()
        flows = c.execute('''SELECT symbol,trading_date,net_amount,available_at FROM quant.stock_money_flow_daily
            WHERE symbol=ANY(%s) AND trading_date=ANY(%s) AND source='longhuvip_main_net'
              AND available_at<=%s ORDER BY symbol,trading_date''', (targets, [date.fromisoformat(d) for d in expected_days], cutoff)).fetchall()
    for bar in bars:
        bars_by.setdefault(bar['symbol'], []).append(dict(bar))
    for flow in flows:
        flows_by.setdefault(flow['symbol'], []).append(dict(flow))
    items, failures = [], []
    for symbol in targets:
        try:
            current = candidates.get(symbol)
            thesis = by_symbol.get(symbol)
            if thesis is None:
                # Only newly produced same-day scans are prospective captures.
                # Replaying a prior session today remains explicitly reconstructed.
                fresh = (source['data_date'] == now.astimezone(SH).date().isoformat()
                         and now - parse_time(source['available_at'], 'source_available_at') <= timedelta(minutes=5)
                         and cutoff_at is None)
                thesis = seed_thesis(current, source['run_id'], source['available_at'], cutoff.isoformat(), deadline,
                                     origin_mode='prospective' if fresh else 'reconstructed')
            series = bars_by.get(symbol, [])
            # Require exact calendar coverage for derived observations.
            missing_sessions = sorted(set(expected_days) - {str(b['trading_date']) for b in series})
            evidence = market_evidence(symbol, series, flows_by.get(symbol, []),
                                       cutoff.isoformat(), expected_date, expected_days)
            intraday_observed_at = None
            try:
                from .bindings import load_bound_plan
                with database.transaction() as c:
                    c.execute('SET TRANSACTION READ ONLY')
                    bound = load_bound_plan(c, thesis['thesis_id'], symbol=symbol, as_of=cutoff)
            except Exception as error:
                logging.getLogger(__name__).exception('thesis_binding_read_failed symbol=%s', symbol)
                bound = {'status': 'unavailable', 'error_type': type(error).__name__}
            if source['kind'] == 'intraday' and current:
                from ..strategy_origin import select_primary_origin
                evaluation_lane = select_primary_origin(current)['lane']
                current_item = current.get('items', {}).get(evaluation_lane)
                if current_item:
                    current_item = {**current_item, 'cutoff_at': source['scan'].get('cutoff')}
                proposed_observed_at = source['scan'].get('observed_at')
                intraday_records = intraday_evidence(symbol, current_item, cutoff.isoformat(), proposed_observed_at)
                evidence.extend(intraday_records)
                intraday_observed_at = proposed_observed_at if intraday_records else None
            def evaluate_with_context(frozen, records, at, previous):
                from ..strategy_origin import select_primary_origin
                result = evaluate_thesis(frozen, records, at, previous)
                result.update(symbol=symbol, name=frozen.get('name') or symbol, data_date=expected_date,
                    source_data_date=source['data_date'],
                    latest_intraday_observed_at=intraday_observed_at,
                    evaluation_lane=select_primary_origin(current)['lane'] if current else frozen.get('origin_primary_lane'),
                    origin_primary_lane=frozen.get('origin_primary_lane'),
                    current_rankings=(current or {}).get('memberships', []), original_rankings=frozen.get('original_rankings', []),
                    missing_sessions=missing_sessions, live_effect='none', decision_binding=False,
                    original_conditions={'confirmation': frozen.get('original_confirmation'), 'invalidation': frozen.get('original_invalidation')})
                from .advisory import project_advisory
                result.update(project_advisory(frozen, result, bound, next_session=str(next_row['calendar_date'])))
                result['input_hash'] = digest([result['input_hash'], result['discipline_binding'], result['scenario_projection']])
                result['evaluation_id'] = digest([result['evaluation_id'], namespace, result['current_rankings'],
                                                   source['run_id'], source['data_date'], result['input_hash']])[:32]
                result['content_hash'] = digest({k: v for k, v in result.items() if k != 'content_hash'})
                return result
            saved = capture_evaluate(database, thesis, evidence, cutoff.isoformat(), source['run_id'], namespace,
                                     evaluator=evaluate_with_context)
            items.append(dict(symbol=symbol, name=thesis.get('name') or symbol, thesis_id=thesis['thesis_id'],
                evaluation_id=saved['evaluation_id'], content_hash=saved['content_hash'], status=saved['status'],
                states=saved['evaluation']['states'], data_date=expected_date,
                claim=thesis.get('claim'), original_structure=thesis.get('original_structure'),
                observations=saved['evaluation']['observations'],
                holding=saved['evaluation']['holding'],
                scenario_projection=saved['evaluation']['scenario_projection'],
                original_rankings=thesis.get('original_rankings', []),
                current_rankings=saved['evaluation']['current_rankings'],
                changes=saved['evaluation']['changes_since_previous'],
                original_confirmation=thesis.get('original_confirmation'), original_invalidation=thesis.get('original_invalidation')))
        except Exception as exc:
            logging.getLogger(__name__).exception('thesis_symbol_failed symbol=%s source_run_id=%s', symbol, source['run_id'])
            failures.append(dict(symbol=symbol, error_type=type(exc).__name__))
    return dict(status='partial' if failures else 'completed', source_run_id=source['run_id'], source_kind=source['kind'],
        cutoff_at=cutoff.isoformat(), data_date=expected_date, namespace=namespace, live_effect='none',
        decision_binding=False, evaluated=len(items), failed=len(failures), items=items, failures=failures, version=VERSION)


def refresh_from_run(database, source_run_id, kind='post_close'):
    """Failure-isolated automatic stage; a real failure remains visible on its run."""
    try:
        receipt = evaluate_source_run(database, str(source_run_id))
    except Exception as exc:
        logging.getLogger(__name__).exception('trade_thesis_refresh_failed source_run_id=%s', source_run_id)
        receipt = dict(status='failed', source_run_id=str(source_run_id), live_effect='none',
                       error_type=type(exc).__name__, version=VERSION)
    with database.transaction() as c:
        if kind == 'post_close':
            from ..short_term_lanes.reports import make_bundle
            row = c.execute('SELECT summary FROM quant.post_close_strategy_runs WHERE run_id=%s FOR UPDATE',
                            (source_run_id,)).fetchone()
            summary = dict(row['summary'])
            if receipt.get('status') == 'completed' and summary.get('strategy_lanes'):
                from .effectiveness import collect_three_arm_snapshot
                # Freeze the first actual capture for this run, never refresh its
                # clock or pretend today's reconstruction existed on signal day.
                if 'trade_thesis_three_arm_snapshot' not in summary:
                    summary['trade_thesis_three_arm_snapshot'] = collect_three_arm_snapshot(
                        summary['strategy_lanes'], receipt, captured_at=receipt['cutoff_at'])
                receipt['three_arm_snapshot'] = summary['trade_thesis_three_arm_snapshot']
            summary['trade_thesis'] = receipt
            if summary.get('strategy_lanes'):
                scan = {**summary['strategy_lanes'], 'trade_thesis': receipt}
                scan['report_bundle'] = make_bundle(scan)
                summary['strategy_lanes'] = scan
            c.execute('UPDATE quant.post_close_strategy_runs SET summary=%s WHERE run_id=%s',
                      (Json(summary), source_run_id))
        else:
            # Preserve original immutable scan input/result hashes; attach stage
            # receipt through the stored thesis evaluations and returned runner receipt.
            if receipt['status'] != 'completed':
                c.execute("UPDATE quant.intraday_strategy_scans SET stage='completed_thesis_partial',error=%s WHERE run_id=%s",
                          (json.dumps({'trade_thesis': receipt.get('error_type') or receipt.get('failures')}), source_run_id))
    return receipt
