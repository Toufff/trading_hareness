"""User-facing notification policy; market evidence and trade rules stay separate."""
from __future__ import annotations

import json
from hashlib import sha256

VERSION = 'notice-v2'
BRIEFING_TIMES = ('10:00','11:35','14:45')


def cadence_status():
    return {'quote_acquisition_seconds':5,'index_acquisition_seconds':15,
            'industry_board_source_seconds':60,'local_evaluation_seconds':1,
            'deepseek_seconds':600,'deepseek_delivery':'verified_condition_changes_only',
            'codex_seconds':None,'codex_delivery':'three_daily_briefings',
            'briefing_times':list(BRIEFING_TIMES),'event_model_followup':False,
            'opening_guard_delivery':'persistent_failure_or_announced_recovery',
            'notification_policy':VERSION}


CONDITION_LABELS = {
    'scope':'跟踪身份', 'quantity':'持仓数量', 'sellable_quantity':'可卖数量',
    'trigger':'触发条件', 'invalidation':'失效条件', 'stop_loss':'止损条件',
    'buy_condition':'买入观察条件', 'buy_conditions':'买入观察条件',
    'conditions':'观察条件', 'invalidation_price':'失效价位',
    'buy_authorized':'候选资格', 'stage':'计划阶段', 'decision':'研究结论',
    'entry_plan':'入场计划', 'exit':'退出条件', 'action_plan':'应对计划',
    'risk_notes':'风险说明', 'company_risk':'公司风险',
    'technical_state':'重点监控量价状态',
}

FOCUS_STATES = {'bullish_confirmation':'量价与动量共同转强',
                'bearish_confirmation':'量价与动量共同转弱',
                'sideways':'持续横盘且量能收缩', 'mixed':'信号混合，暂无确认'}


def condition_values(item):
    facts = item.get('position_or_recommendation') or {}
    result = {'scope':item.get('scope'), **{k:v for k,v in facts.items() if k in CONDITION_LABELS}}
    technical = item.get('focus_technicals') or {}
    if facts.get('monitoring_focus') and technical.get('status') == 'ready':
        result['technical_state'] = FOCUS_STATES.get(technical.get('state'), '状态待确认')
    return result


def condition_signature(item):
    return sha256(json.dumps(condition_values(item), sort_keys=True, default=str,
                             ensure_ascii=False).encode()).hexdigest()


def condition_changes(current, previous):
    if previous is None:
        return []
    before, after = condition_values(previous), condition_values(current)
    changes = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    # A stale/failed minute feed is a data-quality fault, not a bearish turn.
    if 'technical_state' in changes and ('technical_state' not in after or
            ('technical_state' not in before and after['technical_state'] == FOCUS_STATES['mixed'])):
        changes.remove('technical_state')
    return changes


def condition_evidence(current):
    from .presentation import humanize_text
    before = current.get('previous_conditions') or {}
    after = condition_values(current)
    def display(value):
        if value is None:
            return '未提供'
        if isinstance(value,bool):
            return '是' if value else '否'
        if isinstance(value,(dict,list)):
            return '结构化条件（详见工作台）'
        if value in ('holding','recommendation'):
            return '持仓' if value=='holding' else '推荐候选'
        return humanize_text(str(value))[:100]
    return '\n'.join(f"{CONDITION_LABELS[k]}：{display(before.get(k))} → {display(after.get(k))}"
                      for k in current.get('condition_changes',[]))


def guard_notification(verdict, state):
    """Success is silent; only an unrecovered fault, expansion or known recovery speaks."""
    if verdict.status == 'skipped':
        return None
    previous = set(state.get('open_fault') or [])
    if verdict.status == 'ready':
        verified = {x['name'] for x in verdict.checks if x['passed']}
        return 'recovered' if previous and previous <= verified else None
    if not verdict.recovery_attempted:
        return None
    failed = {x['name'] for x in verdict.failed_checks}
    return 'failure' if failed - previous else None
