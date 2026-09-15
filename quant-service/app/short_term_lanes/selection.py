"""Scan-first research selection and provenance; never rank by review availability."""
from __future__ import annotations

import hashlib
import json

from .research_queue import lane_representatives, membership_index

VERSION = 'shared-research-coverage-2026-09-16'
POLICY = ('每条非空策略按条件观察、结构观察、风险观察的显示顺序选取首位，作为同轮公司研究的最低覆盖；'
          '这不是研究上限。推荐池续审、用户跟踪和同类比较可增加复核对象；跨策略重复去重且不算独立利好。')
GROUPS = (
    ('priority', '本轮优先复核：各策略代表'),
    ('additional', '补充候选复核'),
    ('user_followup', '你指定的跟踪'),
    ('risk', '风险案例复核'),
    ('background', '背景资料：不在本轮优先复核中'),
)
OUTCOMES = {'retain_watch': '保留观察', 'downgrade_watch': '降级观察', 'exclude': '移出本轮机会观察'}


def valid_selection(review: dict) -> bool:
    item = review.get('selection')
    return bool(isinstance(item, dict) and item.get('origin') in {'scan', 'user_followup', 'risk', 'background'}
                and all(isinstance(item.get(k), str) and item[k].strip() for k in ('why_now', 'question'))
                and item.get('disposition') in OUTCOMES
                and (item['origin'] != 'user_followup' or str(item.get('request_reference') or '').strip()))


def project(scan: dict, reviews: list[dict]) -> dict:
    memberships = membership_index(scan)
    plan = []
    for target in lane_representatives(scan):
        evidence = target['memberships']
        plan.append({**target, 'as_of_date': scan['as_of_date'], 'scan_version': scan['version'],
                     'evidence_sha256': hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()).hexdigest()})
    targets = {row['symbol']: row for row in plan}
    by_symbol = {r['symbol']: r for r in reviews}
    grouped: dict[str, list[dict]] = {key: [] for key, _ in GROUPS}
    for symbol, review in by_symbol.items():
        valid = valid_selection(review)
        selection = review.get('selection') if valid else {}
        links = memberships.get(symbol, [])
        if not valid:
            key = 'background'
        elif selection['origin'] == 'user_followup':
            key = 'user_followup'
        elif selection['origin'] == 'risk' or (links and all(m['list'] == 'caution_list' for m in links)):
            key = 'risk' if links else 'background'
        elif selection['origin'] == 'scan' and symbol in targets:
            key = 'priority'
        elif selection['origin'] == 'scan' and any(m['list'] == 'selected' for m in links):
            key = 'additional'
        else:
            key = 'background'
        reason = (targets[symbol]['selection_reason'] if key == 'priority' else
                  '；'.join(f"{m['label']}{m.get('state_label') or ('候选' if m['list']=='selected' else '风险名单')}展示第{m['display_rank']}：{m['reason']}" for m in links))
        if key == 'background':
            reason = ('旧记录缺少原始选取理由，不能追认成当日优先复核。' if not valid else
                      '本记录仅作背景，不在本轮优先研究计划中；不能因已有研究而获得优先级。')
        if key == 'user_followup':
            reason = f"用户指定跟踪：{selection['request_reference']}。" + reason
        grouped[key].append({**review, 'memberships': links, 'selection_reason': reason,
                             'outcome_label': OUTCOMES.get(selection.get('disposition'), '历史结论，未核验选取依据')})
    order = {row['symbol']: i for i, row in enumerate(plan)}
    grouped['priority'].sort(key=lambda row: order[row['symbol']])
    # A representative can also be explicitly user-requested or a reviewed
    # risk case. Its real review counts, without duplicating it across groups.
    completed = {r['symbol'] for key, items in grouped.items() if key != 'background'
                 for r in items if r['symbol'] in targets}
    missing = [r['symbol'] for r in plan if r['symbol'] not in completed]
    return dict(review_selection_version=VERSION, review_policy=POLICY, review_plan=plan,
                review_groups=[dict(key=key, label=label, items=grouped[key]) for key, label in GROUPS],
                review_coverage=dict(planned=len(plan), completed=len(completed), missing_symbols=missing,
                                     scope='公司证据复核，不是完整交易授权或收益验证'))
