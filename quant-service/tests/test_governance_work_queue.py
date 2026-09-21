from app.strategy_governance.work_queue import classify, next_action


def item(state='reviewed', **issue):
    return {'id':'x','state':state,'issue':{'scope':'tracking', **issue}}


def test_engineering_is_not_sent_to_small_cap_tuning():
    q=next_action(item(), {'status':'ready'})
    assert q['action']=='isolated_engineering_change'
    assert q['capability']=='developer_worktree'


def test_predictive_and_preference_cannot_share_acceptance():
    assert classify(item(change_kind='ranking_config',proposal_purpose='predictive'))=='predictive'
    assert classify(item(change_kind='ranking_config',proposal_purpose='preference'))=='preference'
    assert next_action(item('ready'),{'status':'ready'})['capability']=='human_only'


def test_collected_evidence_still_requires_independent_review():
    q=next_action(item('discovered'), {'status':'ready'})
    assert q['action']=='independent_review'
    assert q['capability']=='registered_reviewer'
