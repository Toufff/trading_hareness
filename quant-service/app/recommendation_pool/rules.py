"""Pure, replayable recommendation contract. No new alpha weights or broker I/O."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

VERSION = 'recommendation-pool-20260914'
STAGES = {'accumulation', 'initial_breakout', 'strong_pullback', 'post_limit', 'other'}


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def scan_hash(scan):
    # Reports and company-review timestamps can change without changing market inputs.
    # Hash all actual candidates, parameters, coverage and market context instead.
    return digest({k: scan.get(k) for k in ('as_of_date', 'version', 'settings', 'coverage', 'market', 'lanes')})


def intake(scan, run_id, groups, user_tracking=(), next_session=None):
    if scan.get('status') != 'completed' or not scan.get('as_of_date'):
        raise ValueError('scan_not_completed')
    rows, required = {}, set(groups.get('推荐', []))
    for lane in scan.get('lanes', []):
        candidates = lane.get('tracking_candidates')
        if candidates is None:
            raise ValueError('full_candidate_population_missing:' + str(lane.get('key')))
        for rank, c in enumerate(candidates, 1):
            s = c['symbol']
            row = rows.setdefault(s, {'symbol': s, 'name': c.get('name', s), 'memberships': [], 'evidence': deepcopy(c), 'sources': []})
            row['memberships'].append({'lane': lane['key'], 'rank': rank, 'population': len(candidates), 'state': c.get('state')})
            if 'scan' not in row['sources']:
                row['sources'].append('scan')
        representatives = lane.get('selected') or lane.get('observation_list') or lane.get('caution_list') or []
        if representatives:
            required.add(representatives[0]['symbol'])
    for source, symbols in (('previous_recommendation', groups.get('推荐', [])), ('previous_observation', groups.get('观察', [])), ('user_tracking', user_tracking)):
        for s in symbols:
            row = rows.setdefault(s, {'symbol': s, 'name': s, 'memberships': [], 'evidence': {}, 'sources': []})
            row['sources'].append(source)
    # This is a review queue, NOT an investment score. Rank positions only order
    # questions within each strategy; multi-lane hits do not add points.
    rows = sorted(rows.values(), key=lambda r: (r['symbol'] not in required, min((m['rank'] for m in r['memberships']), default=9999), r['symbol']))
    context = {'version': VERSION, 'run_id': str(run_id), 'as_of_date': scan['as_of_date'], 'scan_hash': scan_hash(scan),
               'valid_until': str(next_session) + 'T15:00:00+08:00' if next_session else None,
               'baseline': {g: sorted(set(groups.get(g, []))) for g in ('推荐', '观察')},
               'user_tracking': sorted(set(user_tracking)), 'market': scan.get('market'), 'settings': scan.get('settings'),
               'candidates': rows, 'required_reviews': sorted(required), 'candidate_count': len(rows)}
    context['context_hash'] = digest(context)
    return context


def compile_decision(context, review):
    """Compile reviewed decisions; unknown symbols stay visible, never 'excluded'.

    Every candidate has a screen record. Company verification is mandatory for
    priorities, old recommendations and the scan's first representatives. Other
    unresearched candidates remain explicitly eligible for later challenge.
    """
    if context.get('context_hash') != digest({k: v for k, v in context.items() if k != 'context_hash'}):
        raise ValueError('context_hash_mismatch')
    if review.get('context_hash') != context['context_hash']:
        raise ValueError('review_for_different_context')
    if not review.get('author') or not review.get('market_assessment'):
        raise ValueError('author_and_market_assessment_required')
    rows = {r['symbol']: r for r in context['candidates']}
    decisions, errors = {}, {}
    for item in review.get('items', []):
        s = item['symbol']
        if s in decisions or s in errors or s not in rows:
            raise ValueError('duplicate_or_unknown_symbol:' + s)
        try:
            for key in ('why_now', 'comparison', 'invalidation', 'business', 'company_risk', 'sector_assessment', 'sources'):
                if not item.get(key):
                    raise ValueError('missing_' + key)
            if item.get('data_date') != context['as_of_date']:
                raise ValueError('stale_review')
            if item.get('stage') not in STAGES or item.get('decision') not in ('recommend', 'observe', 'exclude'):
                raise ValueError('invalid_decision_or_stage')
            for src in item['sources']:
                if not str(src.get('url', '')).startswith('https://') or not src.get('published_date') or src['published_date'] > context['as_of_date']:
                    raise ValueError('invalid_source_date')
            if item['decision'] == 'recommend':
                if not item.get('trigger') or not item.get('peer_comparison'):
                    raise ValueError('recommendation_requires_trigger_and_peer_comparison')
                if not isinstance(item.get('priority'), int) or item['priority'] < 1:
                    raise ValueError('positive_editorial_priority_required')
                metrics = rows[s]['evidence'].get('metrics', {})
                if not all(isinstance(metrics.get(k), (int, float)) and math.isfinite(metrics[k]) and metrics[k] > 0 for k in ('close', 'amount')):
                    raise ValueError('current_price_and_amount_required')
            if item['decision'] == 'exclude' and s in context['baseline']['观察']:
                if not all(item.get(k) for k in ('old_thesis', 'invalidated_evidence', 'alternative_value_review')):
                    raise ValueError('observation_removal_requires_full_invalidation_review')
                if s in context['user_tracking']:
                    raise ValueError('user_tracking_requires_explicit_cancellation')
            decisions[s] = {**deepcopy(item), 'name': item.get('name') or rows[s]['name'], 'sources_of_selection': rows[s]['sources'],
                            'memberships': rows[s]['memberships'], 'buy_authorized': False}
        except ValueError as exc:
            errors[s] = str(exc)
    missing = sorted(set(context['required_reviews']) - set(decisions))
    recommended = sorted((v for v in decisions.values() if v['decision'] == 'recommend'), key=lambda v: (v['priority'], v['symbol']))
    budget = review.get('attention_budget', 5)
    if not isinstance(budget, int) or not 1 <= budget <= 12 or len(recommended) > budget:
        raise ValueError('attention_budget_exceeded')
    if len({v['priority'] for v in recommended}) != len(recommended):
        raise ValueError('duplicate_editorial_priority')
    for item in recommended:
        peers = [v for v in recommended if v['symbol'] != item['symbol'] and v.get('sector') == item.get('sector')]
        if peers and not item.get('same_sector_reason'):
            errors[item['symbol']] = 'same_sector_recommendation_requires_reason'
    # Research coverage is not a command to fill the human-readable THS group.
    observed = set(context['baseline']['观察']) | {s for s, v in decisions.items() if v['decision'] == 'recommend' or (v['decision'] == 'observe' and v.get('display_observation') is True)}
    observed -= {s for s, v in decisions.items() if v['decision'] == 'exclude'}
    ready = not errors and not missing
    result = {'version': VERSION, 'run_id': context['run_id'], 'as_of_date': context['as_of_date'],
              'valid_until': context.get('valid_until'),
              'scan_hash': context['scan_hash'], 'context_hash': context['context_hash'], 'baseline': context['baseline'],
              'status': 'ready' if ready else 'partial', 'sync_allowed': ready, 'research_only': True,
              'author': review['author'], 'market_assessment': review['market_assessment'],
              'attention_budget': budget, 'recommended': recommended, 'reviewed': sorted(decisions.values(), key=lambda v: v['symbol']),
              'target_groups': {'推荐': sorted(v['symbol'] for v in recommended), '观察': sorted(observed)},
              'coverage': {'candidates': len(rows), 'reviewed': len(decisions), 'required': len(context['required_reviews']), 'missing': missing, 'errors': errors},
              'screening': [{'symbol': s, 'name': r['name'], 'memberships': r['memberships'],
                             'state': decisions[s]['decision'] if s in decisions else 'not_deep_reviewed',
                             'reason': decisions[s]['comparison'] if s in decisions else '保留完整策略候选及原始量价证据；本次没有完成公司比较，不等于不值得观察或已被排除。'} for s, r in sorted(rows.items())],
              'notice': '优先级是本轮带证据的人工/模型判断，不是新量化分数或收益概率；尚未触发条件的股票不能按收盘价假设成交。'}
    result['decision_id'] = digest(result)
    return result


def current_view(scan, bundle, now=None):
    if not bundle:
        return {'status': 'unavailable', 'sync_allowed': False, 'notice': '本轮尚无正式推荐决策；策略候选不等于推荐池。'}
    if bundle.get('scan_hash') != scan_hash(scan):
        return {**bundle, 'status': 'stale', 'sync_allowed': False, 'notice': '扫描证据已变化，以下是历史推荐，不能同步为本轮结果。'}
    if bundle.get('valid_until') and (now or datetime.now(ZoneInfo('Asia/Shanghai'))) >= datetime.fromisoformat(bundle['valid_until']):
        return {**bundle, 'status': 'expired', 'sync_allowed': False, 'notice': '推荐有效观察窗口已结束；旧名单仅供跟踪，不冒充当前建议。'}
    return bundle


def sync_plan(bundle, current_groups):
    if bundle.get('status') != 'ready' or not bundle.get('sync_allowed'):
        raise ValueError('decision_not_ready')
    actions = []
    for g in ('推荐', '观察'):
        actual = sorted(current_groups.get(g, []))
        target = bundle['target_groups'][g]
        # Idempotent replay is allowed only at the exact initial or final state.
        if actual not in (bundle['baseline'][g], target):
            raise ValueError('concurrent_group_change:' + g)
        if g not in current_groups:
            actions.append({'op': 'ensure_group', 'group': g})
        actions += [{'op': 'add', 'group': g, 'symbol': s} for s in sorted(set(target) - set(actual))]
        actions += [{'op': 'remove', 'group': g, 'symbol': s} for s in sorted(set(actual) - set(target))]
    return {'purpose': 'strategy_refresh', 'request': '同轮推荐决策投射，仅推荐/观察', 'decision_id': bundle['decision_id'],
            'decision_date': bundle['as_of_date'], 'run_id': bundle['run_id'], 'actions': actions}
