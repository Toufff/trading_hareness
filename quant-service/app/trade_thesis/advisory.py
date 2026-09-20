"""Read-model junction: research stays independent of explicit account plans."""
from __future__ import annotations

import json
from math import isfinite

from .contracts import ContractError
from .scenarios import evaluate_scenario_intersection


def project_advisory(thesis, evaluation, bound, *, next_session=None):
    # Repository rows contain UUID/Decimal/timestamptz; the immutable evaluation
    # must remain ordinary JSON so persistence and API hashing agree.
    bound = json.loads(json.dumps(bound, default=str))
    status = bound.get('status', 'unbound')
    plan = bound.get('plan') or {}
    risk = bound.get('risk') or {}
    current = status == 'bound'
    is_holding = current and plan.get('plan_kind') == 'holding'
    notes = {
        'unbound': '未绑定实际入场意图与账户计划；不从研究结论推导卖出动作。',
        'stale': '绑定的纪律计划或评价已过时；保留风险历史，不输出当前卖出股数。',
        'ambiguous_account': '存在多个账户绑定；须明确账户后才展示持仓动作。',
        'unavailable': '账户计划读取失败；研究结论独立保留，不能假装没有风险。',
    }
    state = risk.get('plan_state')
    note = notes.get(status, '已读取显式绑定的账户计划。')
    if current:
        if state in {'exit_signalled', 'reduce_signalled'}:
            note = '已绑定纪律触发' + ('退出' if state == 'exit_signalled' else '减仓') + '信号；研究支持不能覆盖它。'
        elif is_holding:
            note = '已读取有效持仓纪律；今日研究排名变化不自动变成卖出。'
        else:
            note = '绑定的是拟买入纪律，不代表已经持仓。'
    holding = {'status': status, 'note': note, 'account_key': (bound.get('binding') or {}).get('account_key'),
               'plan_id': plan.get('plan_id'), 'plan_state': state, 'hard_risk': risk.get('hard_risk'),
               'is_holding': is_holding, 'action_quantity': None,
               'evaluation_id': (bound.get('latest_evaluation') or {}).get('evaluation_id')}
    lines = []
    if current:
        for line in plan.get('lines') or []:
            try:
                price = float(line.get('price'))
            except (ValueError, TypeError):
                continue
            if not isfinite(price) or price <= 0:
                continue
            lines.append({'id': 'plan-' + str(line.get('kind')), 'kind': 'current_plan',
                'label': str(line.get('label') or line.get('kind')), 'price': price,
                'valid_from': str(plan.get('as_of_at')), 'valid_until': str(plan.get('valid_until')),
                'source_ref': str(plan.get('plan_id')), 'detail': '显式绑定的有效账户纪律，非研究参考线'})
    scenario = thesis.get('scenario')
    if isinstance(scenario, dict):
        try:
            projection = evaluate_scenario_intersection(scenario, evaluation,
                discipline_plan=plan if current else None,
                discipline_evaluation=bound.get('latest_evaluation') if current else None,
                binding=bound.get('binding') if current else None,
                execution_evidence={'next_session': next_session} if next_session else {},
                as_of=evaluation['cutoff_at'])
        except (ContractError, ValueError, TypeError) as error:
            projection = _unstructured(evaluation, ['scenario_invalid:' + type(error).__name__])
    else:
        projection = _unstructured(evaluation, ['approved_structured_scenario_missing'])
    return {'holding': holding, 'scenario_projection': projection, 'chart_lines': lines,
            'discipline_binding': bound}


def _unstructured(evaluation, reasons):
    return {'research_entry_state': evaluation['states']['entry_state'], 'combined_entry_state': 'unknown',
        'missing_context': reasons, 'conflicts': [],
        'execution': {'state': 'unknown', 'verified_fill': False, 'realized_return': None,
                      'reason': '原研究为文字场景，未批准完整可执行合同；保留具体原条件，不擅自将参考价当买点。'},
        'advisory_only': True, 'decision_binding': False, 'live_effect': 'none'}
