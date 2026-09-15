from copy import deepcopy
from datetime import date

import pytest

from app.short_term_lanes.selection import project
from app.short_term_lanes.reviews import validate


def scan():
    def pick(symbol, name):
        return dict(symbol=symbol, name=name, reason='成交额放大且板块同步上涨', rank_score=10,
                    metrics={'amount': 400000000})
    return dict(as_of_date='2026-09-04', version='test', status='completed', lanes=[
        dict(key='trend', label='趋势', total_matches=9,
             selected=[pick('600001.SH', '甲'), pick('600002.SH', '乙')],
             caution_list=[pick('600003.SH', '丙')]),
        dict(key='expansion', label='启动', total_matches=4,
             selected=[pick('600001.SH', '甲')], caution_list=[])])


def review(symbol, origin='scan'):
    return dict(symbol=symbol, name='例', business='主营', risk='风险', conclusion='结论',
        sources=[dict(url='https://static.cninfo.com.cn/test.pdf', published_date='2026-09-01')],
        selection=dict(origin=origin, why_now='本轮复核原因', question='要检验什么', disposition='retain_watch',
                       request_reference='用户指定跟踪' if origin == 'user_followup' else ''))


def test_plan_does_not_depend_on_completed_reviews_or_duplicate_signals():
    first=project(scan(), [])
    second=project(scan(), [review('600002.SH')])
    assert first['review_plan']==second['review_plan']
    assert len(first['review_plan'])==1
    assert len(first['review_plan'][0]['memberships'])==2
    assert second['review_groups'][0]['items']==[]
    assert second['review_coverage']['missing_symbols']==['600001.SH']
    assert second['review_groups'][1]['items'][0]['symbol']=='600002.SH'


def test_groups_follow_evidence_not_symbol_order_or_claimed_origin():
    result=project(scan(), [review('600009.SH'), review('600003.SH', 'risk'),
                            review('600002.SH', 'user_followup'), review('600001.SH')])
    groups={g['key']:g['items'] for g in result['review_groups']}
    assert [x['symbol'] for x in groups['priority']]==['600001.SH']
    assert [x['symbol'] for x in groups['user_followup']]==['600002.SH']
    assert [x['symbol'] for x in groups['risk']]==['600003.SH']
    assert [x['symbol'] for x in groups['background']]==['600009.SH']
    assert groups['priority'][0]['selection_reason']
    assert groups['priority'][0]['memberships'][0]['display_rank']==1


def test_new_reviews_require_selection_but_legacy_is_readable_not_promoted():
    old=review('600001.SH');old.pop('selection')
    with pytest.raises(ValueError): validate(old, date(2026,9,4))
    validate(old, date(2026,9,4), require_selection=False)
    result=project(scan(), [old])
    assert result['review_coverage']['completed']==0
    assert result['review_groups'][-1]['items'][0]['symbol']=='600001.SH'
    bad=review('600001.SH');bad['selection']['question']=' '
    with pytest.raises(ValueError):validate(bad,date(2026,9,4))


def test_incomplete_scan_never_promotes_reviews():
    result=scan();result['status']='data_gap'
    assert project(result,[review('600001.SH')])['review_plan']==[]


def test_selection_projection_is_pure_and_reproducible():
    data=scan();before=deepcopy(data)
    assert project(data,[review('600001.SH')])==project(data,[review('600001.SH')])
    assert data==before


def test_user_requested_representative_counts_without_duplicate_or_false_gap():
    result=project(scan(),[review('600001.SH','user_followup')])
    assert result['review_coverage']['completed']==1
    assert result['review_coverage']['missing_symbols']==[]
    assert sum(len(g['items']) for g in result['review_groups'])==1


def test_report_explains_selection_before_lists():
    from app.short_term_lanes.service import render
    data=scan();data['coverage']={'complete_history':1000,'universe':1000};data['notice']='观察而非买入'
    data.update(project(data,[review('600001.SH')]))
    # Rendering the research section is independent of rendering raw rows.
    data['lanes']=[]
    text=render(data)
    assert '为什么复核：趋势首位代表' in text
    assert '本次具体问题：要检验什么' in text
    assert '复核结果：保留观察' in text
    assert '重点复核结论' not in text
