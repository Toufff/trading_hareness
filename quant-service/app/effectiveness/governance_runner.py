"""Finite, pre-registered shadow ranking experiments. Never execute generated code."""
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, date, timedelta
from pathlib import Path
from statistics import mean,stdev
from math import sqrt
from zoneinfo import ZoneInfo
import hashlib
from .experiments import VARIANTS, score
from .rules import Policy, clean, independent
from .execution import Costs
from ..strategy_governance.rules import digest, require, validate_spec
from ..strategy_governance.configuration import code_fingerprint
from ..strategy_governance.experiment_runner import _baseline

KIND='registered-ranking-effectiveness-v1'


def runner_hash():
    return digest({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).parent.glob('*.py'))})


def supported(spec):
    return spec.get('executor')==KIND and spec.get('variant') in VARIANTS


def prepare_context(database,item,now=None):
    request=item['issue']['effectiveness_request']
    variant=request['variant'];require(variant in VARIANTS,'Unregistered ranking hypothesis')
    lane=item['issue']['scope'];require(request['horizon']==5,'Registered execution protocol requires five sessions')
    now=now or datetime.now(ZoneInfo('Asia/Shanghai'))
    # Future-only fixed interval: old outcomes may motivate a hypothesis, never
    # count as an unseen holdout after looking at them.
    # Allow a week for the separately budgeted review/design/registration roles;
    # a late implementation must be redesigned, not backdated into the holdout.
    start=now.date()+timedelta(days=8);end=start+timedelta(days=730)
    config,generation=_baseline(database);code=code_fingerprint()
    protocol=dict(issue_id=item['id'],variant=variant,lane=lane,profile=request['profile'],
        regime=request['regime'],source_kind=request['source_kind'],registered_at=now.isoformat(),
        start=str(start),end=str(end),policy=asdict(Policy()),costs=asdict(Costs()),runner_hash=runner_hash())
    spec=dict(change_kind='code',executor=KIND,variant=variant,protocol=protocol,
        validation_kind='effectiveness',requires_effectiveness_validation=True,
        input_hash=digest(protocol),baseline_code_hash=code,candidate_code_hash=digest([code,variant,runner_hash()]),
        baseline_config_hash=digest(config),baseline_generation=generation,allowed_strategy_keys=[lane],
        holdout_id='future-shadow:'+digest(protocol),data_start=str(start),data_end=str(end),
        minimum_sessions=20,minimum_observations=100,
        candidate_manifest='Registered immutable ranking adapter '+variant+'; no eligibility or live configuration edits',
        release_plan='Independent validation then human-approved code release; config activation is prohibited',
        criteria=[{'metric':'net_delta_pp','operator':'>=','threshold':.5},
            {'metric':'net_delta_lcb_pp','operator':'>=','threshold':0},
            {'metric':'candidate_net_pct','operator':'>=','threshold':0},
            {'metric':'positive_date_fraction','operator':'>=','threshold':.6},
            {'metric':'complete_date_ratio','operator':'>=','threshold':.9},
            {'metric':'adverse_delta_pp','operator':'>=','threshold':-1},
            {'metric':'eligibility_changed_count','operator':'<=','threshold':0}])
    validate_spec(spec)
    return {'item_id':item['id'],'spec':spec,'spec_hash':digest(spec),'status':'design_context_only',
        'notice':'固定假设与未来验证窗，最低20个非重叠五日样本；不保证赚钱，不修改正式策略。资金/成交口径是明确假设，不是实盘执行。'}


def validate_registered(spec):
    validate_spec(spec);require(supported(spec),'Unsupported executor')
    p=spec['protocol']
    require(digest(p)==spec['input_hash'],'Protocol hash mismatch')
    require(p['registered_at'][:10]<spec['data_start']==p['start'],'Holdout must follow registration')
    require(spec['data_end']==p['end'] and p['variant']==spec['variant'],'Protocol scope mismatch')
    require(spec['allowed_strategy_keys']==[p['lane']],'Cross-strategy mutation prohibited')
    require(p['runner_hash']==runner_hash(),'Registered runner changed; new review required')
    require(code_fingerprint()==spec['baseline_code_hash'],'Baseline code changed; new review required')
    require(spec['candidate_code_hash']==digest([spec['baseline_code_hash'],spec['variant'],runner_hash()]),'Candidate changed')
    require(p['policy']==asdict(Policy()) and p['costs']==asdict(Costs()),'Execution assumptions changed')


def measure_rows(rows, executions, spec, as_of):
    """Pure paired comparison; reject whole date if either selection is unfillable."""
    p=spec['protocol'];days=defaultdict(list);exclusions=Counter();policy=Policy(**p['policy'])
    for r in clean(rows,as_of):
        if (r['timing']=='prospective' and spec['data_start']<=r['signal_date']<=spec['data_end']
            and (r['lane'],r['profile'],r.get('regime'),r.get('source_kind'))==(p['lane'],p['profile'],p['regime'],p['source_kind'])
            and datetime.fromisoformat(r['available_at'])>=datetime.fromisoformat(p['registered_at'])):
            days[r['signal_date']].append(r)
    pairs=[];due=0
    for day,rs in sorted(days.items()):
        if any(r.get('window',{}).get('status')=='not_due' or str(r.get('window',{}).get('date') or '')>as_of for r in rs):
            exclusions['outcome_pending']+=1;continue
        due+=1
        if not all(r.get('window',{}).get('status')=='observed' for r in rs):
            exclusions['outcome_missing']+=1;continue
        if len(rs)<=policy.top_n:exclusions['no_control_group']+=1;continue
        if any(score(r,spec['variant']) is None for r in rs):exclusions['missing_frozen_feature']+=1;continue
        old=([r for r in rs if r.get('selected')] if p['source_kind']=='manual' else sorted(rs,key=lambda r:(r['rank'],r['symbol'])))[:policy.top_n]
        if not old:exclusions['no_registered_selection']+=1;continue
        new=sorted(rs,key=lambda r:(-score(r,spec['variant']),r['symbol']))[:policy.top_n]
        selected={r['origin_id']:r for r in old+new}
        bad=[executions.get(k,{}).get('status','missing_execution_data') for k in selected if executions.get(k,{}).get('status')!='simulated']
        if bad:exclusions.update(set(bad));continue
        def avg(group,key):return mean(executions[r['origin_id']][key] for r in group)
        a,b=avg(old,'net_return_pct'),avg(new,'net_return_pct')
        pairs.append(dict(date=day,end_date=max(executions[k]['exit_date'] for k in selected),
            baseline_net=a,candidate_net=b,delta=b-a,
            adverse_delta=avg(new,'adverse_excursion_pct')-avg(old,'adverse_excursion_pct'),observations=len(selected)))
    sample=independent(pairs)
    def average(key):return mean(r[key] for r in sample) if sample else None
    return {'sessions':[r['date'] for r in sample],'observations':sum(r['observations'] for r in sample),
        'metrics':{'net_delta_pp':average('delta'),'candidate_net_pct':average('candidate_net'),
            'net_delta_lcb_pp':average('delta')-2.1*stdev(r['delta'] for r in sample)/sqrt(len(sample)) if len(sample)>1 else None,
            'baseline_net_pct':average('baseline_net'),'positive_date_fraction':mean(r['delta']>0 for r in sample) if sample else None,
            'adverse_delta_pp':average('adverse_delta'),'complete_date_ratio':len(pairs)/due if due else None,
            'eligibility_changed_count':0},'exclusions':dict(exclusions),'pairs':sample,'due_dates':due,
        'overlap_excluded':len(pairs)-len(sample),'live_effect':'none'}


def run(database,item,as_of):
    from .service import load_rows,load_execution
    from ..strategy_governance.review_evidence import archive_json
    spec=item['experiments'][item['current_experiment']]['spec'];validate_registered(spec)
    active,generation=_baseline(database)
    require(digest(active)==spec['baseline_config_hash'] and generation==spec['baseline_generation'],'Active configuration changed; redesign required')
    rows=load_rows(database,as_of)
    rows=[r for r in rows if spec['data_start']<=r['signal_date']<=min(str(as_of),spec['data_end'])]
    executions=load_execution(database,rows)
    facts=measure_rows(rows,executions,spec,str(as_of))
    raw=archive_json({'rows':rows,'executions':executions,'as_of':str(as_of)})
    evidence={**facts,**{k:spec[k] for k in ('input_hash','baseline_code_hash','candidate_code_hash','holdout_id')},
        'issue_id':item['id'],'validation_kind':'effectiveness','as_of_date':str(as_of),
        'source_artifact':raw,'method':'事前登记的五日等权排名对照，次日开盘模拟、T+1、手数、双边费用和滑点，剔除重叠日期',
        'limitations':'日线代理不能证明盘中条件可成交；涨跌停、停牌及复权不完整会排除整日对照，并降低覆盖率。固定版本样本不足不通过。'}
    artifact=archive_json(evidence)
    return {**evidence,'measurement_artifact':artifact['path'],'measurement_hash':artifact['sha256']}
