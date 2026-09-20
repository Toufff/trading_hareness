from pathlib import Path

from app.short_term_lanes.review_page import document, render, write


def payload(pool_ready=True):
    lane = lambda key, label, selected, caution=(): dict(key=key, label=label, purpose='目的', total_matches=len(selected) + len(caution),
        regime_route={'state': 'neutral', 'priority_weight': 1.0}, empty_reason='本轮无候选',
        selected=[dict(symbol=s, name=n, sector_label='行业甲', reason='突破<平台>', confirmation='站稳', invalidation='跌破',
                       metrics=dict(close=10.5, change_pct=2.5, amount=3.2e8), company_review={'conclusion': '普通观察'} if n == '甲股' else None) for s, n in selected],
        observation_list=[], caution_list=[dict(symbol=s, name=n, sector_label='行业乙', reason='拥挤', confirmation='', invalidation='', metrics=dict(close=3, change_pct=-1)) for s, n in caution])
    lanes = {'as_of_date': '2026-09-17', 'version': 'v-test', 'status': 'completed', 'run_id': 'run-1',
             'market': {'regime': {'label': 'mixed_rotation', 'evidence': ['上涨占比46.9%'], 'research_budget': 0.7}, 'up_fraction': 0.469, 'median_return10': -2.75},
             'coverage': {'universe': 3000, 'complete_history': 2900},
             'review_coverage': {'scope': '公司证据复核', 'planned': 1, 'completed': 1, 'missing_symbols': []},
             'review_groups': [{'key': 'priority', 'label': '本轮优先复核', 'items': [dict(symbol='600001.SH', name='甲股', selection_reason='首位代表', outcome_label='保留观察', conclusion='结论A', risk='风险A')]}],
             'lanes': [lane('trend', '主线趋势', [('600001.SH', '甲股')], [('600002.SH', '乙股')]), lane('event', '事件机会', [])],
             'sector_overview': {'a': dict(label='行业甲', members=8, change_median=1.2, return10_median=5.5, up_fraction=0.75, limit_up=1, flow_3d=1.5e8, relative_strength='strong'),
                                 'tiny': dict(label='太小', members=2, change_median=9, return10_median=9)},
             'event_research': {'status': 'completed', 'summary': '消息摘要', 'events': [dict(fact='事实<1>', transmission='传导', surprise='positive', horizon='短期', importance=0.9)]},
             'followup': {'note': '跟踪说明', 'ledger_rows': 12, 'total': 3, 'items': [dict(symbol='600001.SH', name='甲股', lane='trend', lane_label='主线趋势', source='live_scan', timing='prospective',
                          signal_date='2026-09-16', display_rank=1, expected_sessions=1, path_check='reference_high_touched',
                          windows={'1': {'status': 'observed', 'return_pct': -2.73}, '3': {'status': 'not_due'}, '5': {'status': 'not_due'}, '10': {'status': 'not_due'}})]},
             'report_bundle': {'reports': [{'filename': '2026-09-17_short_term_lanes.md'}]}}
    pool = {'status': 'ready', 'decision_id': 'abcdef1234567890', 'as_of_date': '2026-09-17', 'valid_until': '2026-09-18T15:00:00+08:00',
            'market_assessment': '市场判断', 'notice': '不是买入授权', 'sync_allowed': True,
            'baseline': {'推荐': ['600009.SH']}, 'target_groups': {'推荐': ['600001.SH'], '观察': ['600001.SH', '600009.SH']},
            'recommended': [dict(symbol='600001.SH', name='甲股', priority=1, stage='initial_breakout', sector='行业甲', business='主营设备制造，经营改善', why_now='为什么', trigger='触发', invalidation='失效', peer_comparison='对手比较', company_risk='风险')],
            'reviewed': [dict(symbol='600009.SH', name='旧推荐', decision='observe', comparison='降级理由', invalidation='失效线')]} if pool_ready else {'status': 'unavailable'}
    return {'run': {'run_id': 'run-1', 'as_of_date': '2026-09-17', 'summary': {'strategy_lanes': lanes, 'recommendation_pool': pool}}}


def test_page_leads_with_decision_and_shows_every_lane_tab():
    html = render(payload())
    assert html.index('推荐池已更新') < html.index('<h2>九策略各自结果') < html.index('<h2>市场与板块')
    assert '甲股（600001）' in html and '旧推荐' in html and '从推荐降为观察' in html
    assert 'data-tab="trend"' in html and 'data-tab="event"' in html and '本轮无候选' in html
    assert '突破&lt;平台&gt;' in html and '事实&lt;1&gt;' in html  # escaped
    assert '研究：普通观察' in html and '太小' not in html  # small sectors are not ranked
    assert '主线趋势 / 扫描' in html and '-2.73%' in html
    assert '主营设备制造，经营改善' in html


def test_stale_decision_is_shown_with_its_status_not_hidden():
    data = payload()
    pool = data['run']['summary']['recommendation_pool']
    pool['status'] = 'stale'; pool['notice'] = '扫描证据已变化，以下是历史推荐，不能同步为本轮结果。'
    html = render(data)
    assert '最近一次推荐池决策 abcdef12（状态 stale）' in html and '甲股（优先1）' in html
    assert '扫描证据已变化' in html and '推荐池已更新' not in html and '本轮没有正式推荐池决策' not in html


def test_page_without_decision_is_honest():
    html = render(payload(pool_ready=False))
    assert '本轮没有正式推荐池决策' in html and '推荐池已更新' not in html


def test_document_and_file_are_standalone(tmp_path: Path):
    doc = document(payload())
    assert doc.startswith('<!doctype html>') and doc.count('<title>') == 1 and doc.endswith('</body></html>')
    target = write(tmp_path, payload())
    assert target.name == '2026-09-17_post_close_review.html'
    assert target.read_text(encoding='utf-8') == doc
