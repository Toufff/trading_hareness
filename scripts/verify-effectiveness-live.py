"""Real G PostgreSQL, synthetic anomaly within rollback, never fake live validation."""
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import patch
import json,sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'quant-service'));sys.path.insert(0,str(ROOT/'quant-service/tests'))


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    from dotenv import load_dotenv
    load_dotenv('G:/StockPlatform/config/runtime.env',override=True)
    from app.database import Database
    from app.effectiveness import service
    from app.strategy_governance import repository as repo
    from app.strategy_governance.dispatch_preflight import prepare
    from app.strategy_governance.rules import digest,GovernanceError
    from app.strategy_governance.evidence import verify_measurement
    from test_effectiveness_loop import samples
    db=Database();day=date(2026,9,13);checks={}
    class Rollback(Exception):pass
    def actor(role):return {'id':'acceptance:effectiveness:'+role,'roles':[role]}
    before=repo.resolve_active_config(db);before_ids={i['id'] for i in repo.list_items(db)}
    try:
        real=service.build(db,day,write=False)
        execution=service.load_execution(db,service.load_rows(db,day)[:40])
        checks['real_ledger_read']=real['status']=='completed' and real['row_count']>0
        checks['real_execution_channel_read']=isinstance(execution,dict)
        try:
            with db.transaction() as connection:
                class Isolated:
                    @contextmanager
                    def transaction(self):
                        with connection.transaction():yield connection
                isolated=Isolated()
                with patch.object(service,'load_rows',return_value=samples()):
                    a=service.build(isolated,day);b=service.build(isolated,day)
                checks['anomaly_auto_enqueued_once']=len(a['issue_ids'])==1 and a['issue_ids']==b['issue_ids']
                it=repo.get_issue(isolated,a['issue_ids'][0])
                checks['review_has_original_evidence']=prepare(isolated,it,'reviewer')['status']=='ready'
                it=repo.transition(isolated,it['id'],it['revision'],'review',dict(reproduction='Synthetic rollback acceptance, not an actual reviewer judgement',counterexample='Missing outcomes must not pass',alternative_explanation='Correlation is not causation',verdict='confirmed'),actor('reviewer'))
                context=prepare(isolated,it,'designer');assert context['status']=='ready',context
                claimed=repo.claim_work(isolated,actor('designer'),'designer',item_ids=[it['id']])
                checks['reviewed_effect_issue_is_claimable']=claimed['status']=='claimed'
                frozen=context['design_context']['spec']
                it=repo.transition(isolated,it['id'],it['revision'],'design',dict(change='registered shadow acceptance',tradeoffs='future samples required',failure_condition='missing data',rollback='outer transaction rollback',spec=frozen),actor('designer'))
                checks['registered_implementer_ready']=prepare(isolated,it,'implementer')['status']=='ready'
                it=repo.transition(isolated,it['id'],it['revision'],'experiment',dict(artifact='test registered manifest',artifact_hash=digest(frozen)),actor('implementer'))
                evaluation=prepare(isolated,it,'evaluator');assert evaluation['status']=='ready',evaluation
                evidence=evaluation['measurement'];verify_measurement(evidence)
                it=repo.transition(isolated,it['id'],it['revision'],'observe',evidence,actor('evaluator'))
                claimed=repo.claim_work(isolated,actor('evaluator'),'evaluator',include_waiting=True,item_ids=[it['id']])
                checks['observing_can_receive_future_measurements']=claimed['status']=='claimed'
                checks['future_holdout_not_faked']=it['experiments'][0]['comparison']['status']=='insufficient'
                checks['validator_not_called_for_pending']=prepare(isolated,it,'validator')['status']=='waiting'
                checks['identical_result_not_recounted']=prepare(isolated,it,'evaluator')['reason']=='unchanged_effectiveness_measurement'
                checks['live_config_unchanged_inside']=repo.resolve_active_config(isolated)==before
                try:repo.activate(isolated,it['id'],it['revision'],digest(frozen),actor('human'))
                except GovernanceError:checks['cannot_activate_pending_shadow']=True
                else:checks['cannot_activate_pending_shadow']=False
                raise Rollback()
        except Rollback:pass
        checks['all_test_db_writes_rolled_back']=before_ids=={i['id'] for i in repo.list_items(db)}
        checks['production_configuration_unchanged']=repo.resolve_active_config(db)==before
        receipt={'passed':all(checks.values()),'checks':checks,'real_rows':real['row_count'],
            'real_findings':real['finding_count'],'real_independent_dates':max((g['independent_sessions'] for g in real['groups']),default=0),
            'profitability_validated':False,'scope':'real database read + rollback-only synthetic governance lifecycle; no model review or real profit claim'}
        target=Path('G:/StockPlatform/data/research/effectiveness-acceptance');target.mkdir(parents=True,exist_ok=True)
        (target/'live-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(receipt,ensure_ascii=False))
        if not receipt['passed']:raise SystemExit(1)
    finally:db.close()


if __name__=='__main__':main()
