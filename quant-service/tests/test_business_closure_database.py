"""Actual SQL/persistence in a disposable database only; no production models."""
import os
from pathlib import Path
import pytest

pytestmark=pytest.mark.skipif(not os.getenv('PGDATABASE','').startswith('stock_audit_test_'), reason='disposable database required')


def test_governance_collection_routes_without_self_review(monkeypatch,tmp_path):
    from app.database import Database
    from app.strategy_governance.repository import create_issue,get_issue
    from app.strategy_governance.work_queue import prepare_queue
    monkeypatch.setenv('QUANT_GOVERNANCE_EVIDENCE_ROOT',str(tmp_path))
    db=Database()
    try:
        item=create_issue(db,dict(title='fixture',problem='a missing price',hypothesis='join drops prices',
            scope='tracking',out_of_scope='no live changes',dedupe_key='closure-fixture',
            evidence=[{'coverage':{'missing':1}}]),{'id':'fixture-discovery','roles':['observer']})
        first=prepare_queue(db,[item])
        reread=get_issue(db,item['id'])
        assert reread['state']=='discovered'
        assert first['items'][0]['action']=='independent_review'
        assert reread['review_evidence']['status']=='ready'
        second=prepare_queue(db,[reread])
        assert second['items'][0]['receipt']=='unchanged'
    finally:db.close()


def test_business_matrix_queries_execute_on_real_schema():
    from app.database import Database
    from app.system_business_acceptance import collect
    db=Database()
    try:
        report=collect(db)
        assert report['status']=='partial' # empty DB is not a healthy business
        assert not [c for c in report['checks'] if c['key'].endswith('_read')]
    finally:db.close()


def test_registered_parameter_experiment_measures_without_promoting(monkeypatch,tmp_path):
    import json
    from app.database import Database
    from app.strategy_governance import repository as repo
    from app.strategy_governance.experiment_runner import prepare_context,run_experiment
    from app.strategy_governance.rules import digest,GovernanceError
    from tests.test_strategy_governance import actor
    monkeypatch.setenv('QUANT_GOVERNANCE_EVIDENCE_ROOT',str(tmp_path/'archive'))
    monkeypatch.setenv('QUANT_DATA_DIR',str(tmp_path/'data'))
    db=Database()
    try:
        with db.transaction() as c:
            c.execute("INSERT INTO quant.market_trade_calendar(exchange,calendar_date,is_open,available_at) VALUES('SSE','2026-09-11',true,'2026-01-01Z') ON CONFLICT DO NOTHING")
        source=tmp_path/'frozen.json'
        source.write_text(json.dumps(dict(date='2026-09-11',rows=[],sessions=['2026-09-11'],events={},price_histories={},history_health={})),encoding='utf-8')
        frozen=prepare_context(db,source,{'version':1,'factors':[]},['accumulation'])['spec']
        item=repo.create_issue(db,dict(title='experiment fixture',problem='test execution chain',hypothesis='no effect',
            scope='accumulation',out_of_scope='production',dedupe_key='experiment-fixture',evidence=[{'fixture':True}]),actor('observer'))
        review=dict(reproduction='fixture',counterexample='empty population',alternative_explanation='no data',verdict='confirmed')
        with pytest.raises(GovernanceError):repo.transition(db,item['id'],item['revision'],'review',review,actor('reviewer','observer'))
        item=repo.transition(db,item['id'],item['revision'],'review',review,actor('reviewer'))
        item=repo.transition(db,item['id'],item['revision'],'design',dict(change='no-op fixture',tradeoffs='none',failure_condition='no samples',rollback='no activation',spec=frozen),actor('designer'))
        item=repo.transition(db,item['id'],item['revision'],'experiment',dict(artifact='isolated fixture',artifact_hash=digest(frozen)),actor('implementer'))
        receipt=run_experiment(db,item['id'],source,tmp_path/'measurement')
        measured=json.loads(Path(receipt['evidence_path']).read_text(encoding='utf-8'))
        item=repo.transition(db,item['id'],item['revision'],'observe',measured,actor('evaluator'))
        assert item['state']=='observing'
        assert item['experiments'][0]['comparison']['status']=='insufficient'
        assert repo.resolve_active_config(db) is None
    finally:db.close()
