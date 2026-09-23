from copy import deepcopy
import pytest
from app.recommendation_pool.rules import (intake, compile_decision, sync_plan, current_view, digest,
                                           note_template, recommendation_reference)
from app.recommendation_pool.report import markdown

INFORMATION_URL = 'https://data.eastmoney.com/report/sector-orders'
# S1 leads the market with positive breadth; S2 trails it on both counts, so the
# same helper covers the agreeing and the disagreeing sector branch.
SECTOR_OVERVIEW = {
    'S1': {'sector_key': 'S1', 'label': 'sector1', 'members': 41, 'up_fraction': 0.63, 'return10_median': 6.2,
           'change_median': 1.1, 'limit_up': 2, 'latest_breadth': 0.61, 'recent_breadth': 0.58,
           'breadth_acceleration': 0.07, 'median_change_3d': 0.8, 'flow_3d': 420000000.0,
           'stable_leaders': 4, 'relative_return10': 3.2, 'relative_strength': 'strong'},
    'S2': {'sector_key': 'S2', 'label': 'sector2', 'members': 33, 'up_fraction': 0.36, 'return10_median': 0.4,
           'change_median': -0.6, 'limit_up': 0, 'latest_breadth': 0.34, 'recent_breadth': 0.31,
           'breadth_acceleration': -0.05, 'median_change_3d': -0.4, 'flow_3d': -180000000.0,
           'stable_leaders': 1, 'relative_return10': -2.6, 'relative_strength': 'weak'},
}


def fixture():
    sector = lambda s: 'S1' if int(s) < 6 else 'S2'
    candidate = lambda s: {'symbol': s, 'name': s, 'metrics': {'close': 10, 'amount': 500000000,
                                                              'return_10d': 7.5, 'net5_amount_pct': 1.2},
                           'sector_key': sector(s), 'sector_label': 'sector' + sector(s)[1]}
    scan = {'status': 'completed', 'as_of_date': '2026-09-14', 'version': 'test',
            'market': {'median_return10': 3.0, 'up_fraction': 0.5},
            'sector_overview': deepcopy(SECTOR_OVERVIEW), 'lanes': [
        {'key': 'accumulation', 'tracking_candidates': [candidate(str(i)) for i in range(12)], 'selected': [candidate('0')]},
        {'key': 'trend', 'tracking_candidates': [candidate('8')], 'selected': [candidate('8')]}]}
    c = intake(scan, 'run', {'推荐': ['9'], '观察': ['9', 'retained']}, ['retained'])
    def review(s, decision='observe', priority=1):
        item = {'symbol': s, 'decision': decision, 'data_date': '2026-09-14', 'stage': 'strong_pullback', 'priority': priority,
                'why_now': 'current structure', 'comparison': 'compared against full candidate intake',
                'invalidation': 'structure lost', 'business': 'products', 'company_risk': 'earnings risk',
                'sector_assessment': 'mixed', 'trigger': 'pullback then reclaim', 'peer_comparison': 'peer weaker',
                'sector': s, 'sources': [{'url': 'https://www.cninfo.com.cn/', 'published_date': '2026-08-01'}]}
        if decision == 'recommend':
            item['recommendation_note'] = note(c, s)
        return item
    r = {'author': 'reviewer', 'market_assessment': 'mixed', 'context_hash': c['context_hash'], 'items': [review('0'), review('8', 'recommend'), review('9')]}
    return scan, c, r, review


def note(context, symbol):
    row = next(x for x in context['candidates'] if x['symbol'] == symbol)
    reference = recommendation_reference(context, row)
    overview = reference.get('sector_overview')
    value = {'lane_rankings': [{k: m[k] for k in ('lane', 'rank', 'population')} for m in row['memberships']],
             'rank_assessment': 'first in trend, weak in accumulation; editorial weight on trend',
             'sector_peers': [{'symbol': p['symbol'], 'why_not': 'weaker close and no company support'}
                              for p in reference['required_peers']],
             'sector_view': {'assessment': 'neutral',
                             'trend': 'whole board holds its ten day median and breadth stopped falling',
                             'volume_price': 'sector turnover expands mildly while price base keeps rising',
                             'proxy': {'kind': 'members'} if overview else
                                      {'kind': 'etf', 'symbol': '512480.SH', 'name': '半导体ETF',
                                       'url': 'https://www.sse.com.cn/market/funddata/volumn/etfvolumn/',
                                       'published_date': '2026-09-12'}},
             'information_checks': [{'topic': 'sector news', 'finding': 'order data confirmed',
                                     'url': INFORMATION_URL, 'published_date': '2026-09-10'}],
             'entry_reason': 'strongest verified setup in its sector',
             'priority_reason': 'only recommendation this round',
             'carry_over_reason': 'still beats the new sector challengers'}
    if (overview or {}).get('relative_strength') == 'weak':
        value['sector_disagreement_reason'] = 'system reads the median as weak; the equipment sub-chain turned positive'
    return value


def test_union_keeps_beyond_top_five_and_no_chat_required():
    _, c, r, _ = fixture()
    assert len(c['candidates']) == 13
    assert '11' in {x['symbol'] for x in c['candidates']}
    b = compile_decision(c, r)
    assert b['target_groups']['推荐'] == ['8']
    assert 'retained' in b['target_groups']['观察']
    assert b['status'] == 'ready'
    assert b == compile_decision(deepcopy(c), deepcopy(r))


def test_noon_recommendation_requires_fresh_minute_and_daily_evidence():
    scan, _, _, review_item = fixture()
    context = intake(scan, 'noon-run', {'推荐': ['9'], '观察': ['9', 'retained']},
                     ['retained'], '2026-09-14', source_kind='noon',
                     source_cutoff='2026-09-14T11:30:00+08:00',
                     evidence_eligibility={'8': {'ready': False, 'gaps': ['minute_1130_missing']}})
    item = review_item('8', 'recommend')
    item['recommendation_note'] = note(context, '8')
    item['evidence_available_at'] = '2026-09-14T12:00:00+08:00'
    other = [review_item('0'), review_item('9')]
    review = {'author': 'reviewer', 'market_assessment': 'mixed',
              'reviewed_at': '2026-09-14T12:10:00+08:00',
              'context_hash': context['context_hash'], 'items': [*other, item]}
    blocked = compile_decision(context, review)
    assert blocked['status'] == 'partial'
    assert 'minute_1130_missing' in blocked['coverage']['errors']['8']
    context['evidence_eligibility']['8'] = {'ready': True, 'gaps': []}
    context['context_hash'] = digest({k: v for k, v in context.items() if k != 'context_hash'})
    review['context_hash'] = context['context_hash']
    accepted = compile_decision(context, review)
    assert accepted['status'] == 'ready'
    assert accepted['source_kind'] == 'noon'
    assert accepted['valid_until'] == '2026-09-14T15:00:00+08:00'
    item['evidence_available_at'] = '2026-09-14T12:11:00+08:00'
    assert 'noon_company_evidence_time_invalid' in compile_decision(context, review)['coverage']['errors']['8']


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
    assert '盘后总扫描与推荐池更新' in markdown(b)


def test_human_report_leads_with_total_scan_and_exact_pool_diff():
    scan, c, r, _ = fixture()
    b = compile_decision(c, r)
    text = markdown(b, scan, {'8': '推荐八号', '9': '旧推荐九号'})
    assert text.index('## 一眼结论') < text.index('## 本轮重点')
    assert text.index('## 推荐池更新') < text.index('## 九策略总扫描')
    assert '| 分组 | 更新前 | 更新后 | 新增 | 移除 | 保留 |' in text
    assert '| 推荐 | 1 | 1 | 推荐八号（8） | 旧推荐九号（9） | 无 |' in text
    assert '| 策略 | 全量匹配 | 条件观察 | 结构观察 | 风险观察 | 首位展示 |' in text
    assert '| accumulation | 0 | 1 | 0 | 0 | 0（0） |' in text
    assert '去重候选 12 只' in text
    assert '主营与经营：products' in text


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


def test_structural_observation_is_a_required_floor_not_a_research_cap():
    candidate = lambda s: {'symbol': s, 'name': s, 'reason': 'structure', 'rank_score': 1,
                           'metrics': {'close': 10, 'amount': 500000000}}
    scan = {'status': 'completed', 'as_of_date': '2026-09-15', 'version': 'test', 'lanes': [
        {'key': 'trend', 'label': '趋势', 'total_matches': 2,
         'tracking_candidates': [candidate('a'), candidate('b')], 'selected': [],
         'observation_list': [candidate('a'), candidate('b')], 'caution_list': []},
    ]}
    context = intake(scan, 'run', {'推荐': [], '观察': []})
    assert context['required_reviews'] == ['a']
    assert {row['symbol'] for row in context['candidates']} == {'a', 'b'}


def test_recommendation_research_adapts_to_shared_company_ledger():
    from datetime import date
    from app.short_term_lanes.reviews import from_recommendation, validate
    _, _, _, make_review = fixture()
    item = make_review('600001.SH') | {'name': '六号', 'sources_of_selection': ['scan'],
                               'memberships': [{'lane': 'accumulation'}]}
    adapted = from_recommendation([item])[0]
    validate(adapted, date(2026, 9, 14))
    assert adapted['selection']['origin'] == 'scan'
    assert adapted['business'] == item['business']
    assert adapted['risk'] == item['company_risk']


def test_sector_board_orders_same_sector_candidates_by_within_lane_position():
    _, c, _, _ = fixture()
    board = c['sector_boards']['S2']
    assert [m['symbol'] for m in board['members']] == ['8', '6', '7', '9', '10', '11']
    assert board['members'][0]['best_lane'] == 'trend'


def test_every_recommendation_requires_complete_note():
    _, c, r, _ = fixture()
    del r['items'][1]['recommendation_note']
    b = compile_decision(c, r)
    assert b['coverage']['errors'] == {'8': 'recommendation_note_required'}
    assert b['target_groups']['推荐'] == []


def test_note_rankings_must_match_scan_and_outranking_sector_peers_must_be_answered():
    _, c, r, _ = fixture()
    r['items'][1]['recommendation_note']['lane_rankings'][0]['rank'] = 1
    assert compile_decision(c, r)['coverage']['errors']['8'] == 'recommendation_note:recommendation_note_lane_rankings_do_not_match_scan'
    _, c, r, review = fixture()
    r['items'].append(review('11', 'recommend', 2))
    r['items'][-1]['recommendation_note']['sector_peers'] = [{'symbol': '10', 'why_not': 'only a low ranked peer here'}]
    # Everybody the scan put ahead of 11 must be answered, not just the board top three.
    assert compile_decision(c, r)['coverage']['errors']['11'] == 'recommendation_note:recommendation_note_unanswered_sector_peers:8,6,7,9'


def test_required_peers_add_everyone_ranked_ahead_and_stop_at_the_limit():
    _, c, _, _ = fixture()
    last = next(row for row in c['candidates'] if row['symbol'] == '11')
    reference = recommendation_reference(c, last)
    assert reference['sector_position'] == 6
    assert reference['outranked_count'] == 5
    assert [p['symbol'] for p in reference['required_peers']] == ['8', '6', '7', '9', '10']
    assert {p['scope'] for p in reference['required_peers']} == {'sector'}
    board = c['sector_boards']['S2']
    board['members'] += [{**deepcopy(board['members'][-1]), 'symbol': f'extra{i}', 'name': f'extra{i}',
                          'sector_position': 7 + i} for i in range(4)]
    crowded = {**last, 'symbol': 'extra3', 'memberships': last['memberships']}
    reference = recommendation_reference(c, crowded)
    assert len(reference['required_peers']) == 6
    assert reference['outranked_count'] == 9


def test_small_board_is_filled_from_the_whole_market_instead_of_no_comparison():
    candidate = lambda s, sector: {'symbol': s, 'name': s, 'metrics': {'close': 10, 'amount': 500000000},
                                   'sector_key': sector, 'sector_label': sector}
    scan = {'status': 'completed', 'as_of_date': '2026-09-14', 'version': 'test', 'lanes': [
        {'key': 'accumulation', 'tracking_candidates': [candidate('a', 'S1'), candidate('b', 'S1'),
                                                        candidate('c', 'S1'), candidate('lonely', 'S9')],
         'selected': [candidate('a', 'S1')]}]}
    context = intake(scan, 'run', {'推荐': [], '观察': []})
    reference = recommendation_reference(context, next(r for r in context['candidates'] if r['symbol'] == 'lonely'))
    assert reference['sector_candidates'] == 1 and reference['outranked_count'] == 0
    assert [(p['symbol'], p['scope']) for p in reference['required_peers']] == [('a', 'global'), ('b', 'global'), ('c', 'global')]
    template = note_template(context, 'lonely')
    assert [p['symbol'] for p in template['sector_peers']] == ['a', 'b', 'c']
    assert template['sector_view']['proxy']['kind'] == 'etf'


def test_note_requires_recent_information_and_carry_over_reason():
    _, c, r, review = fixture()
    r['items'][1]['recommendation_note']['information_checks'][0]['published_date'] = '2026-08-01'
    assert compile_decision(c, r)['coverage']['errors']['8'] == 'recommendation_note:recommendation_note_requires_recent_information'
    r['items'][1]['recommendation_note']['information_checks'][0]['published_date'] = '2026-09-15'
    assert 'recommendation_note_future_information' in compile_decision(c, r)['coverage']['errors']['8']
    _, c, r, review = fixture()
    r['items'][2] = review('9', 'recommend', 2)
    del r['items'][2]['recommendation_note']['carry_over_reason']
    assert compile_decision(c, r)['coverage']['errors']['9'] == 'recommendation_note:recommendation_note_missing_carry_over_reason'


def test_every_problem_in_one_note_is_reported_at_once():
    _, c, r, _ = fixture()
    note_value = r['items'][1]['recommendation_note']
    note_value['entry_reason'] = 'too short'
    note_value['lane_rankings'][0]['rank'] = 1
    note_value['sector_peers'][0]['why_not'] = 'weak'
    del note_value['sector_view']
    error = compile_decision(c, r)['coverage']['errors']['8']
    assert error.startswith('recommendation_note:')
    codes = set(error.split(':', 1)[1].split(';'))
    assert 'recommendation_note_reason_too_short:entry_reason' in error
    assert 'recommendation_note_lane_rankings_do_not_match_scan' in codes
    assert 'recommendation_note_missing_sector_view' in codes
    assert any(code.startswith('recommendation_note_reason_too_short:why_not:') for code in codes)


def test_sector_view_must_judge_the_whole_sector_and_fall_back_to_an_etf():
    _, c, r, _ = fixture()
    view = r['items'][1]['recommendation_note']['sector_view']
    view['assessment'] = 'sideways'
    assert compile_decision(c, r)['coverage']['errors']['8'] == 'recommendation_note:recommendation_note_invalid_sector_view'
    _, c, r, _ = fixture()
    r['items'][1]['recommendation_note']['sector_view']['trend'] = 'short trend'
    assert 'recommendation_note_reason_too_short:sector_view.trend' in compile_decision(c, r)['coverage']['errors']['8']
    # No system aggregate for that sector: a thematic ETF proxy becomes mandatory.
    _, c, r, _ = fixture()
    c['sector_overview'] = {}
    c['sector_boards']['S2']['overview'] = None
    c['context_hash'] = digest({k: v for k, v in c.items() if k != 'context_hash'})
    r['context_hash'] = c['context_hash']
    assert 'recommendation_note_sector_etf_proxy_required' in compile_decision(c, r)['coverage']['errors']['8']
    r['items'][1]['recommendation_note']['sector_view']['proxy'] = {
        'kind': 'etf', 'symbol': '600519.SH', 'name': 'not an etf',
        'url': 'https://www.sse.com.cn/x', 'published_date': '2026-09-12'}
    assert 'recommendation_note_invalid_sector_etf_proxy' in compile_decision(c, r)['coverage']['errors']['8']
    r['items'][1]['recommendation_note']['sector_view']['proxy']['symbol'] = '512480.SH'
    r['items'][1]['recommendation_note']['sector_view']['proxy']['name'] = '半导体ETF'
    assert '8' not in compile_decision(c, r)['coverage']['errors']


def test_weak_sector_entry_and_disagreement_with_the_system_must_be_argued():
    _, c, r, _ = fixture()
    r['items'][1]['recommendation_note']['sector_view']['assessment'] = 'weak'
    del r['items'][1]['recommendation_note']['sector_disagreement_reason']
    assert 'recommendation_note_missing_weak_sector_entry_reason' in compile_decision(c, r)['coverage']['errors']['8']
    r['items'][1]['recommendation_note']['weak_sector_entry_reason'] = 'short'
    assert 'recommendation_note_reason_too_short:weak_sector_entry_reason' in compile_decision(c, r)['coverage']['errors']['8']
    # System says weak, author says otherwise: the disagreement itself needs a reason.
    _, c, r, _ = fixture()
    del r['items'][1]['recommendation_note']['sector_disagreement_reason']
    assert compile_decision(c, r)['coverage']['errors']['8'] == 'recommendation_note:recommendation_note_missing_sector_disagreement_reason'


def test_note_template_prefills_system_facts_and_leaves_judgment_empty():
    _, c, _, _ = fixture()
    template = note_template(c, '8')
    assert template['lane_rankings'] == [{'lane': 'accumulation', 'rank': 9, 'population': 12},
                                         {'lane': 'trend', 'rank': 1, 'population': 1}]
    assert [p['symbol'] for p in template['sector_peers']] == ['6', '7', '9']
    assert all(p['why_not'] == '' and p['scope'] == 'sector' for p in template['sector_peers'])
    assert template['sector_view']['proxy'] == {'kind': 'members'}
    assert template['rank_assessment'] == '' and template['information_checks'] == []
    assert 'carry_over_reason' not in template
    assert note_template(c, '9')['carry_over_reason'] == ''
    # The template only has to be answered, never re-derived: filling the blanks
    # of the shipped skeleton must produce an accepted note.
    _, c2, r2, _ = fixture()
    filled = note_template(c2, '8')
    filled.update({k: note(c2, '8')[k] for k in ('rank_assessment', 'entry_reason', 'priority_reason',
                                                 'sector_view', 'information_checks', 'sector_disagreement_reason')})
    for peer in filled['sector_peers']:
        peer['why_not'] = 'lower position and no company evidence this round'
    r2['items'][1]['recommendation_note'] = filled
    assert compile_decision(c2, r2)['coverage']['errors'] == {}


def test_researched_information_reaches_the_company_evidence_ledger():
    from datetime import date
    from app.short_term_lanes.reviews import from_recommendation, validate
    symbols = ['600001.SH', '600002.SH', '600003.SH', '600004.SH']
    candidate = lambda s: {'symbol': s, 'name': '公司' + s[:6], 'sector_key': 'S1', 'sector_label': 'sector1',
                           'metrics': {'close': 10, 'amount': 500000000, 'return_10d': 7.5, 'net5_amount_pct': 1.25}}
    scan = {'status': 'completed', 'as_of_date': '2026-09-14', 'version': 'test',
            'market': {'median_return10': 3.0, 'up_fraction': 0.5},
            'sector_overview': {'S1': deepcopy(SECTOR_OVERVIEW['S1'])},
            'lanes': [{'key': 'trend', 'tracking_candidates': [candidate(s) for s in symbols],
                       'selected': [candidate(symbols[0])]}]}
    context = intake(scan, 'run', {'推荐': [], '观察': []})
    item = {'symbol': symbols[0], 'name': '公司600001', 'decision': 'recommend', 'data_date': '2026-09-14',
            'stage': 'strong_pullback', 'priority': 1, 'why_now': 'current structure',
            'comparison': 'compared against full candidate intake', 'invalidation': 'structure lost',
            'business': 'products', 'company_risk': 'earnings risk', 'sector_assessment': 'mixed',
            'trigger': 'pullback then reclaim', 'peer_comparison': 'peer weaker', 'sector': 'S1',
            'sources': [{'url': 'https://www.cninfo.com.cn/', 'published_date': '2026-08-01'}],
            'recommendation_note': note(context, symbols[0])}
    bundle = compile_decision(context, {'author': 'reviewer', 'market_assessment': 'mixed',
                                        'context_hash': context['context_hash'], 'items': [item]})
    assert bundle['coverage']['errors'] == {}
    assert [s['url'] for s in bundle['recommended'][0]['sources']] == ['https://www.cninfo.com.cn/', INFORMATION_URL]
    adapted = from_recommendation(bundle['reviewed'])[0]
    validate(adapted, date(2026, 9, 14))
    assert INFORMATION_URL in [s['url'] for s in adapted['sources']]
    assert adapted['recommendation_research']['recommendation_note']['entry_reason'] == 'strongest verified setup in its sector'
    assert adapted['recommendation_research']['ranking_reference']['outranked_count'] == 0


def test_context_prepared_before_note_contract_is_rejected():
    _, c, r, _ = fixture()
    old = {k: v for k, v in c.items() if k not in ('sector_boards', 'context_hash')}
    old['context_hash'] = digest(old)
    r['context_hash'] = old['context_hash']
    with pytest.raises(ValueError, match='context_predates_recommendation_note_contract'):
        compile_decision(old, r)


def test_report_shows_system_rankings_peer_metrics_and_whole_sector():
    scan, c, r, _ = fixture()
    text = markdown(compile_decision(c, r), scan)
    assert '#### 推荐说明' in text
    assert '系统排名（由扫描生成，不是人工填写）：accumulation 第9/12；trend 第1/1；同板块位置：sector2 候选第1/6。' in text
    assert '| 同板块对手 | 系统位置 | 10日涨幅 | 5日净流入占比 | 必答 | 为什么不选 |' in text
    assert '| 6（6） | 板块第2 · accumulation 第7/12 | 7.5% | 1.2% | 是 | weaker close and no company support |' in text
    assert ('- 板块整体（系统，全市场 33 只成分）：sector2 10日涨幅中位 0.4% vs 全市场 3.0%，上涨占比 36%，'
            '近3日广度 31%（较前期 -5%），3日资金 -1.80亿，系统判定 weak（参考标签，不是评分）。') in text
    assert '- 板块量价：sector turnover expands mildly while price base keeps rising' in text
    assert f'| sector news | order data confirmed | [2026-09-10]({INFORMATION_URL}) |' in text


def test_report_names_the_candidates_ranked_ahead_of_a_recommendation():
    scan, c, r, review = fixture()
    r['items'].append(review('11', 'recommend', 2))
    r['items'][-1]['same_sector_reason'] = '两只处于不同结构阶段，不是同一个板块投票重复下注'
    r['items'][1]['same_sector_reason'] = '两只处于不同结构阶段，不是同一个板块投票重复下注'
    bundle = compile_decision(c, r)
    assert bundle['coverage']['errors'] == {}
    text = markdown(bundle, scan)
    assert '- 同板块有 5 只候选排在它前面。' in text
