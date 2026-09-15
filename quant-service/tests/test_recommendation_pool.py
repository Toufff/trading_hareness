from copy import deepcopy
import pytest
from app.recommendation_pool.rules import intake, compile_decision, sync_plan, current_view, digest
from app.recommendation_pool.report import markdown


def fixture():
    candidate = lambda s: {'symbol': s, 'name': s, 'metrics': {'close': 10, 'amount': 500000000}}
    scan = {'status': 'completed', 'as_of_date': '2026-09-14', 'version': 'test', 'lanes': [
        {'key': 'accumulation', 'tracking_candidates': [candidate(str(i)) for i in range(12)], 'selected': [candidate('0')]},
        {'key': 'trend', 'tracking_candidates': [candidate('8')], 'selected': [candidate('8')]}]}
    c = intake(scan, 'run', {'推荐': ['9'], '观察': ['9', 'retained']}, ['retained'])
    def review(s, decision='observe', priority=1):
        return {'symbol': s, 'decision': decision, 'data_date': '2026-09-14', 'stage': 'strong_pullback', 'priority': priority,
                'why_now': 'current structure', 'comparison': 'compared against full candidate intake',
                'invalidation': 'structure lost', 'business': 'products', 'company_risk': 'earnings risk',
                'sector_assessment': 'mixed', 'trigger': 'pullback then reclaim', 'peer_comparison': 'peer weaker',
                'sector': s, 'sources': [{'url': 'https://www.cninfo.com.cn/', 'published_date': '2026-08-01'}]}
    r = {'author': 'reviewer', 'market_assessment': 'mixed', 'context_hash': c['context_hash'], 'items': [review('0'), review('8', 'recommend'), review('9')]}
    return scan, c, r, review


def test_union_keeps_beyond_top_five_and_no_chat_required():
    _, c, r, _ = fixture()
    assert len(c['candidates']) == 13
    assert '11' in {x['symbol'] for x in c['candidates']}
    b = compile_decision(c, r)
    assert b['target_groups']['推荐'] == ['8']
    assert 'retained' in b['target_groups']['观察']
    assert b['status'] == 'ready'
    assert b == compile_decision(deepcopy(c), deepcopy(r))


def test_old_recommendation_must_be_reviewed_and_errors_are_localized():
    _, c, r, _ = fixture()
    r['items'][2]['data_date'] = '2026-09-13'
    b = compile_decision(c, r)
    assert b['coverage']['errors'] == {'9': 'stale_review'}
    assert b['coverage']['missing'] == ['9']
    assert b['recommended'][0]['symbol'] == '8'  # Valid research survives.
    with pytest.raises(ValueError, match='not_ready'):
        sync_plan(b, c['baseline'])


def test_no_raw_cross_strategy_score_or_multihit_bonus():
    scan, c, r, _ = fixture()
    scan['lanes'][0]['tracking_candidates'][0]['rank_score'] = 100000
    c2 = intake(scan, 'run', c['baseline'], ['retained'])
    r['context_hash'] = c2['context_hash']
    assert compile_decision(c2, r)['target_groups']['推荐'] == ['8']


def test_same_sector_requires_explanation_not_hard_ban():
    _, c, r, review = fixture()
    r['items'][0] = review('0', 'recommend', 2)
    r['items'][0]['sector'] = r['items'][1]['sector'] = 'PCB'
    assert not compile_decision(c, r)['sync_allowed']
    for x in r['items'][:2]:
        x['same_sector_reason'] = 'different setup and product risk; not two independent sector votes'
    assert compile_decision(c, r)['sync_allowed']


def test_observation_not_removed_for_rank_decline_and_user_protected():
    _, c, r, review = fixture()
    r['items'].append(review('retained', 'exclude'))
    assert not compile_decision(c, r)['sync_allowed']
    for k in ('old_thesis', 'invalidated_evidence', 'alternative_value_review'):
        r['items'][-1][k] = 'documented'
    b = compile_decision(c, r)
    assert 'retained' in b['target_groups']['观察']
    assert b['coverage']['errors']['retained'] == 'user_tracking_requires_explicit_cancellation'


def test_snapshot_concurrency_idempotence_and_untouched_sector_groups():
    _, c, r, _ = fixture()
    b = compile_decision(c, r)
    groups = {**c['baseline'], 'PCB': ['secret_manual_stock'], 'ETF': ['index']}
    plan = sync_plan(b, groups)
    assert {a['group'] for a in plan['actions']} <= {'推荐', '观察'}
    assert sync_plan(b, b['target_groups'])['actions'] == []
    with pytest.raises(ValueError, match='concurrent_group_change'):
        sync_plan(b, {**groups, '观察': ['new_manual_stock']})


def test_changed_scan_is_not_current_and_report_uses_same_bundle():
    scan, c, r, _ = fixture()
    b = compile_decision(c, r)
    assert current_view(scan, b)['status'] == 'ready'
    scan['lanes'][0]['tracking_candidates'][0]['metrics']['close'] = 11
    assert current_view(scan, b)['status'] == 'stale'
    assert b['decision_id'] in markdown(b)
    assert '推荐决策' in markdown(b)


def test_post_scan_research_projection_does_not_make_decision_stale():
    scan, c, r, _ = fixture()
    b = compile_decision(c, r)
    replay = deepcopy(scan)
    replay['lanes'][0]['selected'][0]['company_review'] = {
        'generated_at': '2026-09-14T20:00:00+08:00',
        'conclusion': 'independent company research projection',
    }
    assert current_view(replay, b)['status'] == 'ready'


def test_representative_order_and_event_evidence_remain_hash_material():
    scan, c, r, _ = fixture()
    b = compile_decision(c, r)
    changed = deepcopy(scan)
    changed['lanes'][0]['selected'].append(deepcopy(changed['lanes'][0]['tracking_candidates'][1]))
    changed['lanes'][0]['selected'].reverse()
    assert current_view(changed, b)['status'] == 'stale'
    changed = deepcopy(scan)
    changed['lanes'][0]['selected'][0]['events'] = [{'published_date': '2026-09-14', 'title': 'new event'}]
    assert current_view(changed, b)['status'] == 'stale'


def test_tamper_or_future_source_rejected():
    _, c, r, _ = fixture()
    r['items'][0]['sources'][0]['published_date'] = '2026-09-15'
    assert compile_decision(c, r)['coverage']['errors']['0'] == 'invalid_source_date'
    c['baseline']['推荐'] = []
    with pytest.raises(ValueError, match='context_hash'):
        compile_decision(c, r)


def test_projection_guard_rejects_handwritten_decisions_and_stale_id():
    from app.recommendation_pool.projection_guard import validate_plan
    _, c, r, _ = fixture()
    b = compile_decision(c, r)
    state = {g: {'members': v} for g, v in c['baseline'].items()}
    plan = sync_plan(b, c['baseline'])
    validate_plan(state, plan, lambda: b)
    plan['actions'].append({'op': 'remove', 'group': 'PCB', 'symbol': 'x'})
    with pytest.raises(ValueError, match='actions_do_not_match'):
        validate_plan(state, plan, lambda: b)
    plan = {'actions': [{'op': 'add', 'group': '推荐', 'symbol': 'x'}]}
    with pytest.raises(ValueError, match='formal_decision'):
        validate_plan(state, plan, lambda: b)
    validate_plan(state, {**plan, 'purpose': 'explicit_user_request', 'user_request_reference': 'user explicitly named x'}, lambda: b)


def test_expiry_and_company_review_does_not_fill_software_groups():
    from datetime import datetime
    scan, c, r, review = fixture()
    r['items'].append(review('11'))
    b = compile_decision(c, r)
    assert '11' not in b['target_groups']['观察']
    b['valid_until'] = '2026-09-15T15:00:00+08:00'
    assert current_view(scan, b, datetime.fromisoformat('2026-09-15T15:01:00+08:00'))['status'] == 'expired'
    assert current_view(scan, b, datetime.fromisoformat('2026-09-15T14:59:00+08:00'))['status'] == 'ready'


def test_internal_pool_baseline_does_not_require_ths_snapshot():
    from app.recommendation_pool.repository import latest_target_groups

    class Result:
        def fetchone(self):
            return {'result': {'status': 'ready', 'target_groups': {
                '推荐': ['600001.SH', '600001.SH'], '观察': ['000001.SZ'],
            }}}

    class Connection:
        def execute(self, sql, params):
            assert "recommendation_pool_decisions" in sql
            assert params == ('2026-09-15',)
            return Result()

    class Transaction:
        def __enter__(self): return Connection()
        def __exit__(self, *_): return False

    class Database:
        def transaction(self): return Transaction()

    assert latest_target_groups(Database(), '2026-09-15') == {
        '推荐': ['600001.SH'], '观察': ['000001.SZ'],
    }
