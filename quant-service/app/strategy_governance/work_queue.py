"""Deterministic evidence collection and durable next-action routing.

Collecting evidence is not independent review. This never changes an issue's
review state, grants a role, launches arbitrary code, or activates a strategy.
"""
from pathlib import Path

from .review_evidence import load_review_packet, write_packet, code_excerpt, attach_review_packet
from .rules import digest

SOURCES = {
    'data': ('short_term_lanes/repository.py', ['def load']),
    'tracking': ('short_term_lanes/tracking_repository.py', ['def refresh', 'canonical_bars_daily']),
    'governance_config': ('strategy_governance/configuration.py', ['def code_fingerprint']),
    'presentation': ('short_term_lanes/reports.py', ['def']),
    'execution': ('effectiveness/execution.py', ['def']),
    'all': ('short_term_lanes/price_volume.py', ['def']),
    'conditional entry lifecycle and effectiveness accounting, separate from selection weights':
        ('short_term_lanes/virtual_entry.py', ['def freeze', 'def evaluate']),
    'accumulation only; qualification ablation separate from within-universe reranking':
        ('short_term_lanes/accumulation_rules.py', ['def']),
    **{lane: ('short_term_lanes/rules.py', [lane]) for lane in
       ('accumulation','expansion','pullback','trend','event','relay','contraction','rotation','reclaim')},
}


def sources_for(scope):
    """Exact registry or an explicit list of registered scopes, never arbitrary paths."""
    if scope in SOURCES:
        return [SOURCES[scope]]
    parts = [part.strip() for part in str(scope).split(',')]
    return [SOURCES[part] for part in parts] if parts and all(part in SOURCES for part in parts) else []


def classify(item):
    issue = item['issue']
    if issue.get('effectiveness_request') or issue.get('proposal_purpose') == 'predictive':
        return 'predictive'
    if issue.get('change_kind') == 'ranking_config':
        return 'preference' if issue.get('proposal_purpose') == 'preference' else 'predictive'
    if issue.get('scope') in ('data', 'tracking', 'governance_config', 'presentation', 'execution'):
        return 'engineering'
    return 'engineering' if any('coverage' in e or 'symbols' in e for e in issue.get('evidence', [])) else 'needs_classification'


def next_action(item, packet):
    category = classify(item)
    if item['state'] in ('ready','activated','rejected'):
        return dict(category=category, action='human_activation' if item['state']=='ready' else 'terminal',
                    capability='human_only', retry_on='explicit_user_action', blocker=None)
    if item['state'] == 'discovered':
        return dict(category=category, action='independent_review' if packet['status']=='ready' else 'collect_evidence',
                    capability='registered_reviewer' if packet['status']=='ready' else 'bounded_source_collector',
                    retry_on='new_evidence_hash', blocker=None if packet['status']=='ready' else packet.get('reason'))
    if category == 'engineering':
        return dict(category=category, action='isolated_engineering_change', capability='developer_worktree',
                    retry_on='new_tested_commit_and_rollback_evidence', blocker='engineering_implementation_required')
    if category == 'needs_classification':
        return dict(category=category, action='classify_atomic_issue', capability='independent_reviewer',
                    retry_on='atomic_scope_and_change_kind', blocker='ambiguous_change_kind')
    return dict(category=category, action='registered_shadow_experiment', capability='frozen_experiment_runner',
                retry_on='new_frozen_input_or_exchange_session', blocker=None)


def collect_one(database, item, *, root=None):
    existing = load_review_packet(item)
    if existing['status'] == 'ready' or item['state'] != 'discovered':
        return item, existing
    sources = sources_for(item['issue'].get('scope'))
    evidence = item['issue'].get('evidence') or []
    if not sources or not evidence:
        return item, {'status':'missing','reason':'no_registered_source_collector_for_scope'}
    app = Path(__file__).resolve().parents[1]
    # Explicitly retain original findings and distinguish CURRENT code from
    # historical code. Never call this a reproduction of the past incident.
    reference = write_packet(issue_key=item['issue']['dedupe_key'], question=item['issue']['problem'],
        cases=[{'finding_evidence':e} for e in evidence[:6]],
        code=[code_excerpt(app/source[0], source[1], maximum=45) for source in sources],
        facts={'original_item_id':item['id'], 'original_revision':item['revision'],
               'collection_basis':'preserved_issue_evidence_and_current_source'},
        limitations=['当前源码不是历史源码；复核者必须检验问题是否仍存在。',
                     '未执行任意代码，不伪造反例、复现或收益验证。'], root=root)
    changed = attach_review_packet(database,item['id'],item['revision'],reference,
                                  {'id':'system:bounded-evidence-collector','roles':['observer']})
    return changed, load_review_packet(changed)


def prepare_queue(database, items=None):
    from .repository import list_items
    from .diagnostics import record_diagnostic
    results=[]
    for item in items if items is not None else list_items(database):
        try:
            item, packet = collect_one(database, item)
            action = next_action(item, packet)
            # A bounded follow-up obligation, not a changing timestamp that
            # would manufacture a new diagnostic on every scheduler poll.
            action['review_within_hours'] = 24 if action['action'] != 'terminal' else None
            action['stale_escalation'] = 'operator_review_without_auto_activation'
            payload = dict(status='ready' if not action['blocker'] else 'waiting', role='coordinator',
                reason=action['blocker'] or action['action'], system_owner=action['capability'],
                next_step=action['action'], evidence_hash=digest([item['revision'],packet.get('evidence_hash'),action]),
                model_started=False, work_plan=action)
            receipt = record_diagnostic(database,item['id'],payload)
            results.append({'item_id':item['id'], **action, 'receipt':receipt['status']})
        except (ValueError, OSError, KeyError, TypeError) as exc:
            results.append({'item_id':item['id'],'action':'repair_evidence_collection',
                            'blocker':type(exc).__name__, 'category':classify(item)})
    return {'status':'partial' if any(r.get('blocker') for r in results) else 'ready',
            'items':results,'live_effect':'none','automatic_strategy_activation':False}
