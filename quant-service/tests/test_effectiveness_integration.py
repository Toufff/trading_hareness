from copy import deepcopy
from datetime import date,datetime,timezone
from unittest.mock import patch
import pytest
from test_effectiveness_loop import samples
from app.effectiveness.rules import clean,Policy
from app.effectiveness import pipeline
from app.effectiveness.governance_runner import prepare_context,measure_rows,validate_registered,run
from app.strategy_governance.rules import compare,digest
from app.strategy_governance.evidence import verify_measurement


def item():
    row=samples()[0]
    return {'id':'test','issue':{'scope':row['lane'],'effectiveness_request':{
        'profile':row['profile'],'regime':row['regime'],'source_kind':row['source_kind'],'variant':'attention','horizon':5}}}


def spec():
    with patch('app.effectiveness.governance_runner._baseline',return_value=({'ranking_factors':{'version':1,'factors':[]}},0)):
        return prepare_context(None,item(),datetime(2024,12,20,tzinfo=timezone.utc))['spec']


def executions(rows):
    return {r['origin_id']:{'status':'simulated','net_return_pct':r['window']['return_pct'],
        'exit_date':r['window']['date'],'adverse_excursion_pct':-2} for r in rows}


def test_registered_holdout_measures_and_blocks_missing_or_reconstructed():
    rows=samples();s=spec();before=deepcopy(rows)
    result=measure_rows(rows,executions(rows),s,'2026-09-13')
    assert len(result['sessions'])==25 and rows==before
    e=executions(rows)
    for value in e.values():value['status']='entry_limit_blocked'
    bad=measure_rows(rows,e,s,'2026-09-13')
    assert not bad['sessions'] and bad['metrics']['complete_date_ratio']==0
    for r in rows:r['timing']='reconstructed'
    assert not measure_rows(rows,executions(rows),s,'2026-09-13')['sessions']


def test_measurement_artifact_tamper_and_pending(tmp_path,monkeypatch):
    import app.strategy_governance.review_evidence as archive
    monkeypatch.setattr(archive,'evidence_root',lambda:tmp_path)
    s=spec();it=item();it.update(experiments=[{'spec':s}],current_experiment=0)
    with patch('app.effectiveness.service.load_rows',return_value=[]),patch('app.effectiveness.governance_runner._baseline',return_value=({'ranking_factors':{'version':1,'factors':[]}},0)):
        evidence=run(None,it,date(2026,9,13))
    verify_measurement(evidence)
    assert compare(s,evidence)['status']=='insufficient'
    evidence['sessions']=['2026-01-01']
    with pytest.raises(ValueError):verify_measurement(evidence)


def test_spec_rejects_changed_protocol_and_code():
    s=spec();validate_registered(s)
    s['protocol']['variant']='recent_strength'
    with pytest.raises(ValueError):validate_registered(s)
    s=spec();s['baseline_code_hash']='a'*64
    with pytest.raises(ValueError):validate_registered(s)


def test_failed_feedback_does_not_clear_selection():
    result={'lanes':[{'selected':[{'symbol':'000001.SZ'}]}]};before=deepcopy(result)
    with patch.object(pipeline,'build',side_effect=RuntimeError('fixture')):
        pipeline.attach(None,result,date(2026,9,13))
    assert result['lanes']==before['lanes'] and result['effectiveness']['status']=='failed'


def test_utc_date_is_normalized():
    row=samples()[0];row['available_at']='2024-12-31T17:00:00+00:00'
    assert clean([row],'2026-09-13')[0]['timing']=='prospective'


def test_reports_always_expose_effectiveness_state():
    from app.effectiveness.report import sections
    assert '未接入' in ''.join(sections(None))
    assert '坏了' in ''.join(sections({'status':'failed','reason':'坏了'}))


def test_protocol_retry_reuses_identical_timestamp(tmp_path):
    from app.effectiveness.protocol_cache import context
    it=item();it['revision']=2
    baseline=({'ranking_factors':{'version':1,'factors':[]}},0)
    with patch('app.effectiveness.protocol_cache._baseline',return_value=baseline),patch('app.effectiveness.governance_runner._baseline',return_value=baseline):
        a=context(None,it,tmp_path);b=context(None,it,tmp_path)
    assert a==b


def test_manual_registration_no_backdating_and_retry_identity():
    from app.effectiveness.manual import records
    scan={'as_of_date':'2026-09-11','version':'v','lanes':[{'key':'trend','label':'趋势',
        'tracking_candidates':[{'symbol':s,'name':s,'metrics':{'close':10}} for s in ('A','B')]}]}
    a=records(scan,'trend',['A'],'agent','实际同时登记的理由',now=datetime(2026,9,13,10,tzinfo=timezone.utc))
    b=records(scan,'trend',['A'],'agent','实际同时登记的理由',now=datetime(2026,9,13,11,tzinfo=timezone.utc))
    assert [r['origin_id'] for r in a]==[r['origin_id'] for r in b]
    assert all(r['signal_date']=='2026-09-13' and r['timing']=='prospective' for r in a)
    assert [r['effectiveness']['selected'] for r in a]==[True,False]
    with pytest.raises(ValueError):records(scan,'trend',['A','B'],'agent','reason')


def test_origin_profiles_isolate_actual_code_changes():
    from app.short_term_lanes.tracking_rules import origins
    scan={'as_of_date':'2026-09-11','version':'same label','lanes':[{'key':'trend','label':'趋势','selected':[{'symbol':'A','name':'A','metrics':{'close':10}}]}]}
    a=origins({**scan,'strategy_code_hash':'a'*64},'2026-09-11T16:00:00+08:00','live_scan')[0]
    b=origins({**scan,'strategy_code_hash':'b'*64},'2026-09-11T16:00:00+08:00','live_scan')[0]
    old=origins(scan,'2026-09-11T16:00:00+08:00','live_scan')[0]
    assert len({a['profile'],b['profile'],old['profile']})==3
