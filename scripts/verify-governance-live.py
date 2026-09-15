"""Real PostgreSQL and frozen-market execution; all governance writes rolled back."""
from contextlib import contextmanager
from datetime import datetime,timezone
import json,os,sys,unittest
from pathlib import Path
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'quant-service'))
TARGET=Path('G:/StockPlatform/data/research/2026-09-11-governance-pv-acceptance')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    for line in Path('G:/StockPlatform/config/runtime.env').read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.startswith('#'):
            key,value=line.split('=',1);os.environ[key]=value
    os.environ['RUN_GOVERNANCE_DB_TESTS']='1'
    from tests.test_strategy_governance_live import GovernanceDatabaseTests
    result=unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(GovernanceDatabaseTests))
    if not result.wasSuccessful():raise SystemExit(1)
    from app.database import Database
    from app.strategy_governance import repository as repo
    from app.strategy_governance.rules import digest
    from app.strategy_governance.experiment_runner import prepare_context,run_experiment
    from app.strategy_governance.frozen_inputs import freeze_input
    original=json.loads((TARGET/'frozen-input.json').read_text(encoding='utf-8'))
    archived=freeze_input(day=original['date'],rows=original['rows'],sessions=original['sessions'],
        events=original['events'],price_histories=original['price_histories'],history_health=original['history_health'])
    assert archived['status']=='ready'
    assert json.loads(Path(archived['input_path']).read_bytes())['rows']==original['rows']
    db=Database()
    class Rollback(Exception):pass
    def actor(role):return {'id':'acceptance:'+role,'roles':[role]}
    before=repo.resolve_active_config(db)
    output=TARGET/('parent-experiment-'+uuid4().hex[:8])
    try:
        try:
            with db.transaction() as connection:
                class Isolated:
                    @contextmanager
                    def transaction(self):
                        with connection.transaction():yield connection
                isolated=Isolated()
                item=repo.create_issue(isolated,dict(title='真实实验宿主验收',problem='测试排序范围隔离',hypothesis='仅横盘排序改变，其他策略不变',scope='accumulation',out_of_scope='生产配置',dedupe_key='test:'+uuid4().hex,evidence=[{'kind':'engineering_acceptance'}]),actor('observer'))
                item=repo.transition(isolated,item['id'],1,'review',dict(reproduction='工程验收夹具，不是策略改进提案',counterexample='跨策略影响应失败',alternative_explanation='排序与入选分别测试',verdict='confirmed'),actor('reviewer'))
                context=prepare_context(isolated,archived['input_path'],{'version':1,'factors':[{'key':'small_cap','weight':.12,'strategies':['accumulation']}]},['accumulation'])
                item=repo.transition(isolated,item['id'],item['revision'],'design',dict(change='仅验证排序因子宿主',tradeoffs='不检验收益',failure_condition='范围越界或入选变化',rollback='外层事务回滚',spec=context['spec']),actor('designer'))
                item=repo.transition(isolated,item['id'],item['revision'],'experiment',dict(artifact='deterministic runner',artifact_hash=digest(context['spec'])),actor('implementer'))
                receipt=run_experiment(isolated,item['id'],archived['input_path'],output)
                evidence=json.loads((output/'evidence.json').read_text(encoding='utf-8'))
                item=repo.transition(isolated,item['id'],item['revision'],'observe',evidence,actor('evaluator'))
                assert item['experiments'][0]['comparison']['status']=='passed'
                assert evidence['sessions']==['2026-09-11']
                assert repo.resolve_active_config(isolated)==before
                raise Rollback()
        except Rollback:pass
        assert repo.resolve_active_config(db)==before
        status={'passed':True,'at':datetime.now(timezone.utc).isoformat(),'database_lifecycle':True,
            'real_experiment':str(output),'frozen_input':archived,'metrics':evidence['metrics'],'independent_sessions':1,
            'writes_rolled_back':True,'production_config_unchanged':True,'profitability_validated':False}
        (TARGET/'parent-governance-live-receipt.json').write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(status,ensure_ascii=False))
    finally:db.close()


if __name__=='__main__':main()
