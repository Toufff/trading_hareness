"""Bound event context and bind model interpretation to deterministic evidence."""
from __future__ import annotations
from hashlib import sha256
import json
from typing import Any

from ..agent_paper.model import ModelFailure
from .notice_policy import condition_changes, condition_signature, condition_values, condition_evidence, CONDITION_LABELS

DELTA_KINDS = {'event','discipline','ten_minute'}
FACT_KEYS = {'quantity','sellable_quantity','position_weight_pct','trigger','invalidation',
             'risk','risk_notes','action','action_plan','buy_condition','buy_conditions',
             'entry','entry_plan','exit','stop_loss','decision','stage','sector','why_now',
             'trade_thesis','thesis','conditions','invalidation_price','buy_authorized','company_risk',
             'monitoring_focus'}


def compact_facts(facts):
    return {k:v for k,v in facts.items() if k in FACT_KEYS}


def scope_signature(item):
    feature = (item.get('windows') or {}).get('60') or {}
    # No price penny/noise or wording fingerprint. Changes are categorical.
    change = feature.get('price_change_pct') or 0
    value = [feature.get('pressure_state'),feature.get('volume_state'),
             'up' if change>=.15 else 'down' if change<=-.15 else 'flat',
             item.get('scope'),condition_values(item).get('technical_state'),
             compact_facts(item.get('position_or_recommendation') or {})]
    return sha256(json.dumps(value,sort_keys=True,default=str,ensure_ascii=False).encode()).hexdigest()


def prepare_delta(payload, baseline):
    events = payload.get('recent_events') or []
    symbols = {e.get('symbol') or (e.get('payload') or {}).get('symbol') for e in events}
    is_event = payload.get('report_kind') in {'event','discipline'}
    changed = []
    for item in payload['scope']:
        previous = baseline.get(item['symbol'])
        changes = condition_changes(item, previous)
        # Price/flow changes already have a deterministic card. The periodic
        # model only explains independently changed plan facts, never a rewording.
        if payload.get('report_kind') == 'ten_minute' and not changes:
            continue
        ready = ((item.get('windows') or {}).get('60') or {}).get('status') == 'ready'
        if is_event and item['symbol'] not in symbols:
            continue
        if is_event and previous and scope_signature(item)==scope_signature(previous) and not any(
                e.get('source')=='discipline' for e in events):
            continue
        if not is_event and (not ready or (previous and scope_signature(item)==scope_signature(previous))):
            continue
        item = dict(item)
        item['previous_notified'] = {'windows':previous.get('windows'), 'quote':previous.get('quote'),
                                    'notice_key':previous.get('_notification_key')} if previous else None
        item['condition_changes'] = changes
        item['previous_conditions'] = condition_values(previous) if previous else {}
        item['position_or_recommendation'] = compact_facts(item['position_or_recommendation'])
        changed.append(item)
    market_events = [] if payload.get('report_kind') == 'ten_minute' else [
        e for e in events if e.get('source') in {'market_index','sector'}]
    return {**payload,'scope':changed,'market_context':payload['market_context'] if market_events else {},
            'delta_only':True,'market_events':market_events}


def bind_output(output: dict[str,Any], payload: dict[str,Any]) -> dict[str,Any]:
    """Reject identity/role mismatches, then render facts from our computations.

    The LLM supplies only a bounded interpretation/conditional observation;
    neither it nor its free prose can replace deterministic price/volume facts.
    """
    expected = {x['symbol']:x for x in payload['scope']}
    result = dict(output)
    for group,role in (('holding_focus','holding'),('recommendation_focus','recommendation')):
        for item in output.get(group) or []:
            source = expected.get(item.get('symbol'))
            if source is None or source['name'] != item.get('name') or source['scope'] != role:
                raise ModelFailure('identity_or_scope_mismatch',str(item.get('symbol')))
    if not payload.get('delta_only'):
        return result
    items = []
    for item in output.get('delta_items') or []:
        source = expected.get(item.get('symbol'))
        if source is None or source['name'] != item.get('name'):
            raise ModelFailure('identity_or_scope_mismatch',str(item.get('symbol')))
        feature = (source.get('windows') or {}).get('60') or {}
        previous = ((source.get('previous_notified') or {}).get('windows') or {}).get('60') or {}
        from .presentation import pressure_evidence
        change = feature.get('pressure_text','区间证据不足')
        if previous.get('pressure_text'):
            change = previous['pressure_text']+' → '+change
        if source.get('condition_changes'):
            change = '、'.join(CONDITION_LABELS[k] for k in source['condition_changes'])+'已更新'
        action = str(item.get('action') or '')[:140]
        # A prospective candidate cannot receive a sell instruction.
        if source['scope'] != 'holding' and any(x in action for x in ('减仓','清仓','卖出','止损卖','退出持仓')):
            raise ModelFailure('candidate_sell_instruction',item['symbol'])
        # Every numeric claim must already exist in the bounded input, including price lines.
        import re
        if re.search(r'[零一二三四五六七八九十百千\d.]+\s*(?:分钟|秒|倍|[%％]|基点|手)',action):
            raise ModelFailure('model_repeated_computed_metric',item['symbol'])
        known = set(re.findall(r'\d+(?:\.\d+)?',json.dumps(source,ensure_ascii=False,default=str)))
        if any(x not in known for x in re.findall(r'\d+(?:\.\d+)?',action)):
            raise ModelFailure('unsupported_action_number',item['symbol'])
        if not action.strip():
            continue
        items.append({'symbol':source['symbol'],'name':source['name'],'scope':source['scope'],
                      'change':change,'evidence':pressure_evidence(feature), 'action':action,
                      'condition_evidence':condition_evidence(source)})
    result['delta_items'] = items[:3]
    result['market_summary'] = ''
    result['market_delta'] = '；'.join(str(e.get('summary') or '') for e in payload.get('market_events',[]))[:250]
    result['should_notify'] = output.get('should_notify') is True and bool(items or result['market_delta'])
    clocks = [x.get('quote',{}).get('observed_at') for x in expected.values() if x.get('quote')]
    result['data_as_of'] = max((x for x in clocks if x),default=payload['as_of'])
    result['headline'] = '；'.join(x['name']+'：'+x['change'] for x in items[:2]) or result['market_delta']
    result['state_fingerprint'] = sha256(json.dumps(
        [(x['symbol'],condition_signature(expected[x['symbol']]),
          (expected[x['symbol']].get('previous_notified') or {}).get('notice_key')) for x in items] +
        [('market',result['market_delta'])],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return result
