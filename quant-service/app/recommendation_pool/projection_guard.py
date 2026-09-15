"""THS boundary: a strategy plan must match the current persisted decision."""
import json
from urllib.request import urlopen
from .rules import sync_plan


def validate_plan(state, plan, fetch=None):
    touched = any(a.get('group') in ('推荐', '观察') or a.get('from') in ('推荐', '观察') or a.get('to') in ('推荐', '观察') for a in plan.get('actions', []))
    if not touched and plan.get('purpose') != 'strategy_refresh':
        return
    if plan.get('purpose') == 'explicit_user_request':
        if not plan.get('user_request_reference'):
            raise ValueError('explicit_user_request_reference_required')
        return  # Explicit user edits are not falsely labelled strategy research.
    if plan.get('purpose') != 'strategy_refresh':
        raise ValueError('formal_decision_or_explicit_user_request_required')
    if fetch is None:
        def fetch():
            with urlopen('http://127.0.0.1:5681/api/v1/strategy/post-close/watchlist/latest', timeout=15) as r:
                return json.load(r).get('recommendation_pool') or {}
    bundle = fetch()
    if not bundle.get('decision_id') or plan.get('decision_id') != bundle['decision_id']:
        raise ValueError('not_the_current_persisted_decision')
    expected = sync_plan(bundle, {g: x['members'] for g, x in state.items()})
    if plan.get('run_id') != expected['run_id'] or plan.get('decision_date') != expected['decision_date']:
        raise ValueError('decision_source_mismatch')
    if plan.get('actions') != expected['actions']:
        raise ValueError('actions_do_not_match_formal_decision')
