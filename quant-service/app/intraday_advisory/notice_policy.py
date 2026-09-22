"""User-facing notification policy; market evidence and trade rules stay separate."""
from __future__ import annotations

import json
from hashlib import sha256

VERSION = 'notice-v2'
CONDITION_LABELS = {
    'scope':'跟踪身份', 'quantity':'持仓数量', 'sellable_quantity':'可卖数量',
    'trigger':'触发条件', 'invalidation':'失效条件', 'stop_loss':'止损条件',
    'buy_condition':'买入观察条件', 'buy_conditions':'买入观察条件',
    'conditions':'观察条件', 'invalidation_price':'失效价位',
    'buy_authorized':'候选资格', 'stage':'计划阶段', 'decision':'研究结论',
    'entry_plan':'入场计划', 'exit':'退出条件', 'action_plan':'应对计划',
    'risk_notes':'风险说明', 'company_risk':'公司风险',
}


def condition_values(item):
    facts = item.get('position_or_recommendation') or {}
    return {'scope':item.get('scope'), **{k:v for k,v in facts.items() if k in CONDITION_LABELS}}


def condition_signature(item):
    return sha256(json.dumps(condition_values(item), sort_keys=True, default=str,
                             ensure_ascii=False).encode()).hexdigest()


def condition_changes(current, previous):
    if previous is None:
        return []
    before, after = condition_values(previous), condition_values(current)
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


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
