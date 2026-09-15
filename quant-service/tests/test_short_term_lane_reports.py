from copy import deepcopy
from hashlib import sha256

from app.short_term_lanes.reports import make_bundle, write_bundle
from app.short_term_lanes.selection import project


def example():
    def pick(symbol, name, reason):
        return dict(symbol=symbol,name=name,reason=reason,rank_score=9,sector_label='行业甲',
                    confirmation='缩量承接后转强',invalidation='结构被破坏',caution='不追高',expiry='下一交易日',
                    metrics=dict(close=10,change_pct=2,amount=400000000,turnover=3))
    one=pick('600001.SH','股票甲','放量突破');two=pick('600002.SH','股票乙','回踩缩量')
    data=dict(as_of_date='2026-09-04',version='test',status='completed',
              coverage=dict(universe=1000,complete_history=990,verified_event_symbols=0),
              market=dict(up_fraction=.6,median_return10=2),notice='观察不是买入',settings={},sessions=['2026-09-04'],
              lanes=[dict(key='expansion',label='放量启动',purpose='观察启动',status='completed',total_matches=5,
                          selected=[one],caution_list=[],empty_reason='没有匹配'),
                     dict(key='pullback',label='强势回踩',purpose='观察回踩',status='completed',total_matches=3,
                          selected=[two,one],caution_list=[],empty_reason='没有匹配'),
                     dict(key='event',label='事件机会',purpose='核查事件',status='completed',total_matches=0,
                          selected=[],caution_list=[],empty_reason='没有匹配')])
    review=dict(symbol=one['symbol'],name=one['name'],business='主营甲',risk='风险甲',conclusion='保留观察甲',
                selection=dict(origin='scan',why_now='启动代表',question='盈利是否改善',disposition='retain_watch'),
                sources=[dict(url='https://static.cninfo.com.cn/one.pdf',published_date='2026-09-01')])
    data.update(project(data,[review]))
    return data


def test_one_report_per_strategy_plus_overview_and_shared_identity(tmp_path):
    data=example();before=deepcopy(data);bundle=make_bundle(data)
    assert data==before
    assert [r['key'] for r in bundle['reports']]==['overview','expansion','pullback','event']
    assert all(r['as_of_date']==data['as_of_date'] for r in bundle['reports'])
    assert all(r['content_sha256']==sha256(r['markdown'].encode()).hexdigest() for r in bundle['reports'])
    write_bundle(tmp_path,data,bundle)
    for report in bundle['reports']:
        assert (tmp_path/report['filename']).read_text(encoding='utf-8')==report['markdown']


def test_strategy_is_self_contained_and_does_not_import_unrelated_stock():
    reports={r['key']:r for r in make_bundle(example())['reports']}
    text=reports['expansion']['markdown']
    assert '股票甲（600001）' in text and '主营甲' in text and '盈利是否改善' in text
    assert '缩量承接后转强' in text and '结构被破坏' in text
    assert '股票乙' not in text
    assert '返回总报告' in text and '完整历史' in text
    assert reports['pullback']['review']['review_coverage']['completed']==0
    assert '强势回踩条件观察展示第2' in reports['pullback']['markdown']


def test_empty_report_and_no_double_vote_overview():
    bundle=make_bundle(example());reports={r['key']:r for r in bundle['reports']}
    assert '没有匹配' in reports['event']['markdown']
    assert '已核验事件覆盖 0 只' in reports['event']['markdown']
    assert len(bundle['overlaps'])==1
    assert bundle['overlaps'][0]['symbol']=='600001.SH'
    overview=reports['overview']['markdown']
    assert '跨策略重复与分歧' in overview and '不是独立利好计票' in overview
    assert all(r['filename'] in overview for r in reports.values() if r['key']!='overview')
    assert '完整候选与观察条件' not in overview


def test_incomplete_data_not_reported_as_empty_success():
    data=example();data['status']='data_gap'
    for lane in data['lanes']:
        lane.update(status='data_gap',selected=[],caution_list=[],empty_reason='历史缺口')
    data.update(project(data,[]))
    bundle=make_bundle(data)
    assert all('数据不足' in r['markdown'] for r in bundle['reports'])


def test_repeated_generation_stable_and_no_path_traversal():
    import pytest
    assert make_bundle(example())==make_bundle(example())
    data=example();data['lanes'][0]['key']='../escape'
    with pytest.raises(ValueError):make_bundle(data)
    data['lanes'][0]['key']='overview'
    with pytest.raises(ValueError):make_bundle(data)


def test_audit_rejects_missing_changed_and_mixed_generation(tmp_path):
    from app.short_term_lanes.report_audit import check_bundle
    data=example();data['report_bundle']=make_bundle(data)
    write_bundle(tmp_path,data,data['report_bundle'])
    assert all(check_bundle(data,tmp_path).values())
    report=data['report_bundle']['reports'][1]
    path=tmp_path/report['filename']
    path.write_text('wrong generation',encoding='utf-8')
    assert not check_bundle(data,tmp_path)['bundle_disk_equals_api']
    path.unlink()
    assert not check_bundle(data,tmp_path)['bundle_unique_safe_files']
    data['notice']='changed input'
    assert not check_bundle(data,tmp_path)['bundle_source_hash']


def test_independent_discovery_audit_accepts_own_lane_and_rejects_foreign_symbol(tmp_path):
    from app.short_term_lanes.report_audit import check_bundle
    data = example()
    lane = data['lanes'][0]
    extra = {**deepcopy(lane['selected'][0]), 'symbol':'000636.SZ', 'name':'风华高科'}
    lane['observation_list'] = [extra, lane['selected'][0]]
    extra_review = {**deepcopy(data['review_groups'][0]['items'][0]),
                    'symbol': extra['symbol'], 'name': extra['name']}
    data.update(project(data, [data['review_groups'][0]['items'][0], extra_review]))
    data['report_bundle'] = make_bundle(data)
    write_bundle(tmp_path, data, data['report_bundle'])
    assert all(check_bundle(data,tmp_path).values())
    report = data['report_bundle']['reports'][1]
    assert report['result_summary']['rows'][0]['symbol'] == '000636.SZ'
    report['result_summary']['rows'][0]['symbol'] = '600999.SH'
    assert not check_bundle(data,tmp_path)['bundle_result_summary_scoped']


def test_overlap_conflict_keeps_risk_visible():
    data=example()
    row=data['lanes'][1]['selected'].pop()
    data['lanes'][1]['caution_list']=[row]
    data.update(project(data,[]))
    bundle=make_bundle(data)
    assert '分歧' in bundle['overlaps'][0]['interpretation']
    assert {m['state'] for m in bundle['overlaps'][0]['memberships']}=={'条件观察','风险观察'}


def test_single_strategy_reorders_reviews_by_its_own_priority():
    data=example()
    first=data['review_groups'][0]['items'][0]
    second={**deepcopy(first),'symbol':'600002.SH','name':'股票乙'}
    data.update(project(data,[first,second]))
    report=next(r for r in make_bundle(data)['reports'] if r['key']=='pullback')
    company=next(g for g in report['review']['review_groups'] if g['key']=='company')
    assert [r['symbol'] for r in company['items']]==['600002.SH','600001.SH']
    assert report['markdown'].index('### 股票乙') < report['markdown'].index('### 股票甲')


def test_results_and_actual_research_precede_process_in_every_report():
    data = example()
    for report in make_bundle(data)['reports']:
        text = report['markdown']
        headings = [line for line in text.splitlines() if line.startswith('## ')]
        assert headings[0] == '## 本次结论'
        assert text.index('## 本次结论') < text.index('## 数据与筛选说明')
        if report['key'] != 'event':
            assert text.index('保留观察甲') < text.index('为什么优先复核')
        if report['key'] not in ('overview', 'event'):
            assert text.index('确认条件：缩量承接后转强') < text.index('完整历史')
    first = make_bundle(data)['reports'][1]['result_summary']['rows'][0]
    assert first['conclusion'].endswith('保留观察甲')
    assert first['confirmation'] == data['lanes'][0]['selected'][0]['confirmation']


def test_overview_tables_explain_every_displayed_stock_even_without_company_review():
    reports = {r['key']: r for r in make_bundle(example())['reports']}
    overview = reports['overview']['markdown']
    # Both reviewed and unreviewed rows retain their strategy reason and
    # executable confirmation/invalidation evidence instead of becoming a
    # bare-name list in compact mode.
    assert '| 股票 | 状态 | 为什么关注 | 公司复核 |' in overview
    assert '| 股票 | 确认条件 | 放弃条件 | 风险与有效期 |' in overview
    assert '股票乙（600002）' in overview
    assert '回踩缩量' in overview
    assert '缩量承接后转强' in overview
    assert '结构被破坏' in overview
    assert '筛选层：本轮未列入公司比较范围' in overview


def test_caution_only_result_is_visible_without_promoting_it():
    data = example()
    lane = data['lanes'][0]
    lane['caution_list'] = lane['selected']
    lane['selected'] = []
    data.update(project(data, []))
    report = make_bundle(data)['reports'][1]
    summary = report['result_summary']
    assert '风险观察' in summary['conclusion']
    assert summary['rows'][0]['state'] == '风险观察'
    assert summary['rows'][0]['conclusion'] is None
    assert '待他人完成' not in report['markdown']
    assert report['markdown'].index('股票甲（600001）') < report['markdown'].index('完整历史')


def test_gap_and_empty_results_are_front_page_not_buried():
    data = example()
    event = make_bundle(data)['reports'][-1]
    assert '已核验事件覆盖 0 只' in event['result_summary']['conclusion']
    assert not event['result_summary']['rows']
    data['lanes'][0]['status'] = 'data_gap'
    assert '数据不足' in make_bundle(data)['reports'][1]['result_summary']['conclusion']
