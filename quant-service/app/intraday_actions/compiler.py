"""Compile frozen structural evidence into a separate research minute plan.

No daily condition is promoted. Entry rules require an explicitly versioned
minute policy; existing minute hard stops keep their exact price/count. The
caller must retain the FIRST compiled result for its source/policy/session:
recompiling on every tick moves the forward-only eligibility boundary forever.
No database, model, broker, clock or hidden parameter defaults live here.
"""
from datetime import datetime, time, timedelta
from hashlib import sha256
import json
from math import isfinite
from statistics import fmean
from zoneinfo import ZoneInfo

from .contracts import ACTIONS
from ..trade_discipline.risk_policy import PER_NAME_LOSS_TOLERANCE_PCT, POLICY_VERSION

VERSION = 'intraday-action-compiler-v1'
SHANGHAI = ZoneInfo('Asia/Shanghai')


def _timestamp(value):
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return stamp if stamp.tzinfo is not None and stamp.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


def _number(value, *, zero=False):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if isfinite(result) and (result >= 0 if zero else result > 0) else None


def _integer(value, *, zero=False):
    number = _number(value, zero=zero)
    return int(number) if number is not None and number.is_integer() else None


def _minute_baseline(rows, at, window):
    """Use ONLY the final complete, contiguous pre-compilation minute window.

    Excluding malformed rows would hide a gap, so unusable evidence fails as a
    whole. Volume unit is the contracts.py share unit, never day-to-date volume.
    """
    if not isinstance(window, int) or isinstance(window, bool) or window < 3:
        return None
    usable = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        stamp = _timestamp(row.get('end_at'))
        if (stamp is None or stamp >= at or stamp.astimezone(SHANGHAI).date() != at.astimezone(SHANGHAI).date()
                or row.get('completed') is not True or not row.get('source')):
            return None
        volume = _number(row.get('volume'), zero=True)
        if volume is None:
            return None
        usable.append((stamp, volume, str(row['source'])))
    if len(usable) < window:
        return None
    stamps = [r[0] for r in usable]
    if stamps != sorted(set(stamps)):
        return None
    tail = usable[-window:]
    if (any(b[0]-a[0] != timedelta(minutes=1) for a, b in zip(tail, tail[1:]))
            or at-tail[-1][0] > timedelta(minutes=2) or len({r[2] for r in tail}) != 1):
        return None
    mean = fmean(r[1] for r in tail)
    if mean <= 0:
        return None
    return {'value': mean, 'unit': 'shares_per_completed_minute', 'count': window,
            'start_at': tail[0][0].isoformat(), 'end_at': tail[-1][0].isoformat(),
            'source': tail[0][2], 'volumes': [r[1] for r in tail]}


def compile_action_plan(discipline_plan, *, account_key, symbol, role, snapshot_id,
                        decision_id, compiled_at, minutes=(), policy=None, local_preview=False):
    """Return ``{plan, blockers, provenance}``; inputs are never mutated.

    ``policy`` (or frozen metrics.intraday_policy) requires version/source,
    confirmation_minutes, volume_baseline_minutes, min_volume_ratio,
    sector_min_change, risk_budget_pct. source_rank/source_rank_source come from
    an existing ranking, not a fabricated cross-strategy score.
    An empty rules map is an honest non-actionable draft, never ready coverage.
    """
    blockers = []
    raw = dict(discipline_plan) if isinstance(discipline_plan, dict) else {}
    at = _timestamp(compiled_at)
    if local_preview and at is not None and raw.get('plan_key') and not raw.get('plan_id'):
        raw['plan_id'] = 'local-preview:' + str(raw['plan_key'])
        raw['created_at'] = at.isoformat()
    metrics = raw.get('metrics') or {}
    sizing = raw.get('sizing') or {}
    provenance = {'compiler_version': VERSION, 'source_plan_id': str(raw.get('plan_id') or ''),
                  'source_inputs_hash': raw.get('inputs_hash'),
                  'source_created_at': str(raw.get('created_at') or ''),
                  'source_as_of_at': str(raw.get('as_of_at') or ''),
                  'source_metrics_date': metrics.get('trading_date')}

    def block(action, code):
        value = {'action': action, 'code': code}
        if value not in blockers:
            blockers.append(value)

    def fail(code):
        for action in ACTIONS:
            block(action, code)
        return {'plan': None, 'blockers': blockers, 'provenance': provenance}

    if at is None:
        return fail('compilation_time_invalid')
    if not raw.get('plan_id') or not raw.get('inputs_hash'):
        return fail('source_plan_identity_missing')
    if raw.get('account_key') != account_key or raw.get('symbol') != symbol:
        return fail('source_plan_identity_mismatch')
    expected_kind = {'holding': 'holding', 'recommendation': 'new_buy'}.get(role)
    if expected_kind is None or raw.get('plan_kind') != expected_kind:
        return fail('source_plan_role_mismatch')
    quality = raw.get('quality')
    if (raw.get('status') != 'active' or not isinstance(quality, list) or not quality
            or any(not isinstance(q, dict) or q.get('passed') is not True for q in quality)):
        return fail('source_plan_quality_not_accepted')
    created, source_at, expires = (_timestamp(raw.get(k)) for k in ('created_at', 'as_of_at', 'valid_until'))
    if created is None or source_at is None or expires is None or max(created, source_at) > at or expires <= at:
        return fail('source_plan_not_available_or_expired')
    position = raw.get('position') or {}
    if role == 'holding' and (not snapshot_id or str(position.get('snapshot_id') or '') != str(snapshot_id)):
        return fail('source_snapshot_mismatch')
    if role == 'recommendation' and (not decision_id or f'recommendation_decision:{decision_id}' not in
                                      (raw.get('evidence_refs') or [])):
        return fail('source_decision_mismatch')
    close = datetime.combine(at.astimezone(SHANGHAI).date(), time(15), SHANGHAI)
    if at >= close:
        return fail('minute_plan_session_expired')
    selected_policy = policy if policy is not None else metrics.get('intraday_policy')
    p = dict(selected_policy) if isinstance(selected_policy, dict) else {}
    rules = {}
    rank = _integer(raw.get('source_rank', p.get('source_rank')))
    rank_source = raw.get('source_rank_source') or p.get('source_rank_source')
    if role == 'recommendation' and (rank is None or not rank_source):
        block('first_buy', 'source_rank_missing')
    hard_stop = _number(sizing.get('hard_stop'))
    plan = {'version': VERSION, 'created_at': at.isoformat(), 'expires_at': min(expires, close).isoformat(),
            'quality_status': 'accepted', 'account_key': account_key, 'symbol': symbol,
            'hard_stop': hard_stop, 'allow_buy': False, 'allow_add': False,
            'risk_budget_pct': float(PER_NAME_LOSS_TOLERANCE_PCT / 100), 'rules': rules,
            'source_rank': rank, 'source_rank_source': rank_source,
            'decision_id': decision_id if role == 'recommendation' else None,
            'snapshot_id': snapshot_id, 'research_only': True, 'live_effect': 'none',
            'buy_authorized': False, 'provenance': provenance}
    plan.update(mode='local_preview' if local_preview else 'research_shadow', production_eligible=False)
    provenance['risk_policy'] = {'version': POLICY_VERSION, 'source': 'trade_discipline.risk_policy',
                                 'loss_tolerance_percent': float(PER_NAME_LOSS_TOLERANCE_PCT)}

    # Keep only a source minute line whose ENTIRE condition we can preserve.
    if role == 'holding':
        held = _integer(position.get('quantity'))
        for index, line in enumerate(raw.get('lines') or []):
            confirm = line.get('confirm') or {}
            count = _integer(confirm.get('bars'))
            stop = _number(line.get('price'))
            if (line.get('kind') == 'hard_stop' and line.get('metric') == 'minute_close'
                    and line.get('op') == '<' and confirm.get('basis') == 'minute'
                    and count is not None and count >= 3 and stop is not None and held is not None
                    and not line.get('extra') and (line.get('action') or {}).get('type') == 'exit_all'):
                rules['exit'] = {'basis': 'completed_1m', 'kind': 'hard_stop', 'confirmation_minutes': count,
                                 'reference': stop, 'quantity': held, 'version': VERSION,
                                 'source_field': f'lines[{index}]'}
                break
        if 'exit' not in rules:
            block('exit', 'source_minute_exit_missing')
        block('reduce', 'explicit_minute_reduction_required')

    action = 'add' if role == 'holding' else 'first_buy'
    before = len([b for b in blockers if b['action'] == action])
    if not p.get('version') or not p.get('source'):
        block(action, 'explicit_intraday_policy_required')
    count = _integer(p.get('confirmation_minutes'))
    ratio = _number(p.get('min_volume_ratio'))
    sector_min = p.get('sector_min_change')
    try:
        sector_min = float(sector_min) if not isinstance(sector_min, bool) else None
    except (TypeError, ValueError):
        sector_min = None
    if (count is None or count < 3 or ratio is None or sector_min is None or not isfinite(sector_min)
            or sector_min < 0):
        block(action, 'intraday_policy_parameters_missing')
    if p.get('risk_budget_pct') is not None and _number(p['risk_budget_pct']) != plan['risk_budget_pct']:
        block(action, 'intraday_risk_policy_conflict')
    day = str(metrics.get('trading_date') or '')
    try:
        settled = datetime.fromisoformat(day).date() < at.astimezone(SHANGHAI).date()
    except ValueError:
        settled = False
    if not settled or metrics.get('forming') or metrics.get('synthetic_bars', 0):
        block(action, 'settled_structure_required')
    field = 'entry.lane_reference' if role == 'recommendation' else (
        'ma5' if raw.get('stage') in {'trend_hold', 'pullback_hold'} else
        'prior_high' if raw.get('stage') in {'breakout_hold', 'base_platform'} else None)
    ref = _number((metrics.get('entry') or {}).get('lane_reference') if role == 'recommendation'
                  else metrics.get(field))
    cap = _number(sizing.get('sizing_price'))
    if ref is None or cap is None or hard_stop is None or not hard_stop < ref <= cap:
        block(action, 'source_buy_zone_missing_or_invalid')
    target = _integer(sizing.get('recommended_shares'), zero=True)
    current = _integer(sizing.get('current_shares'), zero=True)
    quantity = target-current if target is not None and current is not None else None
    if quantity is None or quantity <= 0:
        block(action, 'source_incremental_quantity_unavailable')
    if _number(sizing.get('risk_per_trade_pct')) != float(PER_NAME_LOSS_TOLERANCE_PCT):
        block(action, 'source_sizing_risk_policy_not_current')
    if role == 'holding' and any((line.get('action') or {}).get('type') == 'block_add'
                                 or line.get('kind') == 'no_add' for line in raw.get('lines') or []):
        block(action, 'source_no_add_not_released')
    if role == 'holding' and (p.get('thesis_valid') is not True or not p.get('thesis_source')):
        block(action, 'explicit_add_thesis_confirmation_required')
    baseline = _minute_baseline(minutes, at, p.get('volume_baseline_minutes'))
    if baseline is None:
        block(action, 'completed_minute_volume_baseline_unavailable')
    if len([b for b in blockers if b['action'] == action]) == before and before == 0:
        rules[action] = {'basis': 'completed_1m', 'kind': 'breakout', 'confirmation_minutes': count,
                         'reference': ref, 'max_buy_price': cap, 'quantity': quantity,
                         'version': VERSION, 'volume_baseline': baseline['value'],
                         'volume_baseline_evidence': baseline, 'min_volume_ratio': ratio,
                         'sector_min_change': sector_min, 'source_field': f'metrics.{field}',
                         'quantity_source': 'sizing.recommended_shares-sizing.current_shares',
                         'max_buy_price_source': 'sizing.sizing_price',
                         'policy_source': p['source'], 'policy_version': p['version']}
        plan['allow_buy'] = True
        if action == 'add':
            plan['allow_add'] = True
            plan['thesis_valid'] = True
            provenance['thesis_source'] = p['thesis_source']
    # An explicit research reduction is a separate breakdown thesis, never a
    # concentration/risk-cap reduction inferred from the source share count.
    reduction = p.get('reduction') or {}
    if role == 'holding' and reduction:
        field = reduction.get('reference_field')
        reference = _number(metrics.get(field)) if field in {'ma5', 'ma10', 'prior_high', 'recent_low'} else None
        qty = _integer(reduction.get('quantity'))
        if (settled and not metrics.get('synthetic_bars', 0) and not metrics.get('forming')
                and p.get('source') and p.get('version') and count is not None and count >= 3
                and reduction.get('source') and reference is not None and hard_stop is not None
                and reference > hard_stop and qty is not None and qty <= (_integer(position.get('quantity')) or 0)):
            rules['reduce'] = {'basis': 'completed_1m', 'kind': 'breakdown', 'confirmation_minutes': count,
                               'reference': reference, 'quantity': qty, 'version': VERSION,
                               'source_field': f'metrics.{field}', 'policy_source': reduction['source']}
            blockers[:] = [b for b in blockers if b != {'action': 'reduce', 'code': 'explicit_minute_reduction_required'}]
        else:
            block('reduce', 'explicit_reduction_evidence_invalid')
    t = p.get('t') or {}
    for action in ('t_buy_first', 't_sell_first'):
        if role != 'holding' or not t:
            block(action, 'explicit_t_plan_required')
            continue
        low_field, high_field = t.get('range_low_field'), t.get('range_high_field')
        allowed_fields = {'recent_low', 'prior_high', 'low10_close', 'high10_close', 'prev_low', 'prev_high'}
        low = _number(metrics.get(low_field)) if low_field in allowed_fields else None
        high = _number(metrics.get(high_field)) if high_field in allowed_fields else None
        cap_field = t.get('max_buy_price_field')
        t_cap = _number(metrics.get(cap_field)) if cap_field in allowed_fields | {'ma5', 'ma10', 'prev_close'} else None
        floor_field = t.get('min_sell_price_field')
        sell_floor = (_number(metrics.get(floor_field))
                      if floor_field in allowed_fields | {'ma5', 'ma10', 'prev_close'} else None)
        qty = _integer(t.get('quantity'))
        profit = _number(t.get('min_net_profit'), zero=True)
        pullback = _number(t.get('min_pullback_pct'))
        if (not settled or metrics.get('forming') or metrics.get('synthetic_bars', 0) or low is None
                or high is None or hard_stop is None or not hard_stop < low < high):
            block(action, 'frozen_t_range_missing_or_invalid')
        if t_cap is None or low is None or high is None or not low <= t_cap < high:
            block(action, 'frozen_t_buy_cap_missing_or_invalid')
        if action == 't_sell_first' and (sell_floor is None or low is None or high is None
                                         or not low < sell_floor <= high):
            block(action, 'frozen_t_sell_floor_missing_or_invalid')
        if action == 't_buy_first' and (p.get('thesis_valid') is not True or not p.get('thesis_source')):
            block(action, 'explicit_temporary_add_permission_required')
        if p.get('risk_budget_pct') is not None and _number(p['risk_budget_pct']) != plan['risk_budget_pct']:
            block(action, 'intraday_risk_policy_conflict')
        if qty is None or qty > (_integer(position.get('sellable_quantity'), zero=True) or 0):
            block(action, 't_quantity_exceeds_verified_old_position')
        if (not t.get('source') or profit is None or pullback is None or not p.get('source') or not p.get('version')
                or count is None or count < 3 or baseline is None or ratio is None
                or sector_min is None or not isfinite(sector_min) or sector_min < 0):
            block(action, 'explicit_t_minute_policy_required')
        costs = p.get('costs') or {}
        cost_at = _timestamp(costs.get('as_of'))
        cost_keys = ('commission_rate', 'min_commission', 'stamp_tax_sell_rate', 'transfer_rate', 'slippage_bps')
        if (not costs.get('source') or cost_at is None or cost_at > at
                or any(_number(costs.get(k), zero=True) is None for k in cost_keys)):
            block(action, 'explicit_t_costs_required')
        if any((line.get('action') or {}).get('type') == 'block_add' or line.get('kind') == 'no_add'
               for line in raw.get('lines') or []):
            block(action, 'source_no_add_not_released')
        if any(b['action'] == action for b in blockers):
            continue
        common = {'basis': 'completed_1m', 'confirmation_minutes': count, 'quantity': qty, 'version': VERSION,
                  'policy_source': t['source'], 'range_source': [f'metrics.{low_field}', f'metrics.{high_field}']}
        buy_rule = {**common, 'kind': 'pullback_reclaim', 'reference': low, 'max_buy_price': t_cap,
                    'max_buy_price_source': f'metrics.{cap_field}',
                    'volume_baseline': baseline['value'], 'volume_baseline_evidence': baseline,
                    'min_volume_ratio': ratio, 'sector_min_change': sector_min}
        sell_rule = {**common, 'kind': 'breakout', 'reference': high}
        if action == 't_buy_first':
            rules[action] = {**buy_rule, 'target_price': high, 'min_net_profit': profit, 'second_leg': sell_rule}
            # Temporary inventory permission is independent of whether a
            # permanent add was compiled; account risk/cash still gate it.
            plan['allow_add'] = True
            plan['thesis_valid'] = True
            provenance['thesis_source'] = p['thesis_source']
        else:
            rules[action] = {**common, 'kind': 'exhaustion', 'reference': high, 'peak_reference': high,
                             'min_pullback_pct': pullback, 'target_price': low, 'min_net_profit': profit,
                             'min_sell_price': sell_floor, 'min_sell_price_source': f'metrics.{floor_field}',
                             'second_leg': buy_rule}
        plan['allow_buy'] = True
        provenance['t_costs'] = costs
    provenance['policy'] = p
    # Includes the frozen first compilation boundary, not the caller's future ticks.
    plan['id'] = sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    return {'plan': plan, 'blockers': blockers, 'provenance': provenance}


__all__ = ['VERSION', 'compile_action_plan']
