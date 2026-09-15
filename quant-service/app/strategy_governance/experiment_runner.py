"""Deterministic same-input factor experiments, never strategy edits or profit claims."""
from dataclasses import replace
from datetime import date
import hashlib
import json
from pathlib import Path
from .configuration import code_fingerprint, environment_config
from .repository import resolve_active_config, get_issue
from .rules import require, digest, validate_spec, check_config_scope, compare
from ..ranking_factors import load_profile
from ..short_term_lanes.rules import LANES, Settings, screen


METRICS = {'eligibility_changed_count', 'out_of_scope_changed_count', 'buy_authorized_count',
           'baseline_candidate_count', 'candidate_candidate_count', 'rank_changed_count', 'scan_failure_count'}
ENGINEERING_CRITERIA = [
    {'metric':'eligibility_changed_count','operator':'<=','threshold':0},
    {'metric':'out_of_scope_changed_count','operator':'<=','threshold':0},
    {'metric':'buy_authorized_count','operator':'<=','threshold':0},
    {'metric':'scan_failure_count','operator':'<=','threshold':0},
]


def runner_fingerprint():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def strict_input_day(value):
    require(isinstance(value,str),'Input date must be a string')
    if len(value)==8 and value.isdigit():value=value[:4]+'-'+value[4:6]+'-'+value[6:]
    require(len(value)==10,'Strict ISO/compact date required')
    parsed=date.fromisoformat(value)
    require(str(parsed)==value,'Strict ISO/compact date required')
    return parsed


def read_frozen(path):
    path = Path(path)
    raw = path.read_bytes()
    frozen = json.loads(raw.decode('utf-8-sig'))
    require(isinstance(frozen,dict) and all(k in frozen for k in ['date','rows','sessions','events','price_histories','history_health']), 'Unsupported frozen input schema')
    day = date.fromisoformat(frozen['date'])
    require(str(day)==frozen['date'], 'ISO as-of date required')
    from ..short_term_lanes.event_time import resolve_cutoff
    resolve_cutoff(frozen['date'],frozen.get('information_cutoff'))
    require(all(strict_input_day(x)<=day for x in frozen['sessions']), 'Future sessions leaked into snapshot')
    require(all(strict_input_day(r.get('trade_date'))<=day for r in frozen['rows']), 'Future rows leaked into snapshot')
    for bars in frozen['price_histories'].values():
        for bar in bars:
            require(strict_input_day(bar.get('date') or bar.get('trade_date'))<=day,'Future or malformed OHLC leaked into snapshot')
    return frozen, hashlib.sha256(raw).hexdigest()


def _baseline(database):
    active=resolve_active_config(database)
    require(not active or active['status']=='active', 'Active governed profile has stale code; resolve it before designing an experiment')
    return (active['config'],active['generation']) if active else (environment_config(),0)


def _calendar(database, day):
    with database.transaction() as c:
        row=c.execute('''SELECT bool_or(is_open) AS is_open FROM quant.market_trade_calendar
             WHERE exchange IN ('SSE','SZSE') AND calendar_date=%s''',(day,)).fetchone()
    require(row and row['is_open'] is True, 'Frozen as-of date is not a verified exchange session')
    return {'date':day, 'source':'quant.market_trade_calendar', 'exchanges':['SSE','SZSE'], 'is_open':True}


def prepare_context(database, input_path, candidate_profile, allowed_keys):
    frozen,input_hash=read_frozen(input_path)
    config,generation=_baseline(database)
    fingerprint=code_fingerprint()
    from .source_archive import archive_sources
    source_archive=archive_sources()
    require(source_archive['code_hash']==fingerprint and source_archive['runner_hash']==runner_fingerprint(),'Source changed during experiment preparation')
    spec=dict(change_kind='ranking_config',validation_kind='engineering',input_hash=input_hash,runner_hash=runner_fingerprint(),
        source_archive=source_archive,
        baseline_code_hash=fingerprint,candidate_code_hash=fingerprint,baseline_config_hash=digest(config),
        baseline_generation=generation,allowed_strategy_keys=list(allowed_keys),holdout_id='engineering-snapshot:'+input_hash,
        data_start=frozen['date'],data_end=frozen['date'],minimum_sessions=1,minimum_observations=1,
        criteria=[dict(c) for c in ENGINEERING_CRITERIA],candidate_config={'ranking_factors':candidate_profile})
    validate_spec(spec)
    changed=check_config_scope(spec,config)
    return dict(status='design_context_only',spec=spec,spec_hash=digest(spec),baseline_config=config,
        calendar=_calendar(database,frozen['date']),affected_strategies=changed,
        notice='只是设计上下文，未审核/未入实验。当前一次收盘快照仅算1个独立交易日，不证明收益有效性。')


def _run(frozen, config):
    settings=replace(Settings(), ranking_factors=load_profile(config['ranking_factors'],{x[0] for x in LANES}))
    return screen(frozen['rows'],frozen['sessions'],frozen['date'],events=frozen['events'],
        price_histories=frozen['price_histories'],history_health=frozen['history_health'],settings=settings,
        information_cutoff=frozen.get('information_cutoff'))


def measure(baseline, candidate, allowed_keys):
    """Only factual code-output metrics; no LLM-supplied metric or invented return."""
    old={l['key']:l for l in baseline['lanes']};new={l['key']:l for l in candidate['lanes']}
    keys=set(old)|set(new)
    def members(lane): return {r['symbol'] for r in lane.get('tracking_candidates',[])}
    def ranked(lane): return sorted(lane.get('tracking_candidates',[]),key=lambda r:(-r['rank_score'],r['symbol']))
    changed=outside=ranks=authorized=before=after=0
    details=[]
    for key in sorted(keys):
        a,b=old.get(key,{}),new.get(key,{})
        aa,bb=members(a),members(b)
        count=len(aa^bb); changed+=count;before+=len(aa);after+=len(bb)
        oldrank={r['symbol']:i for i,r in enumerate(ranked(a),1)}
        newrank={r['symbol']:i for i,r in enumerate(ranked(b),1)}
        rankchanges=sum(oldrank[s]!=newrank[s] for s in aa&bb);ranks+=rankchanges
        # Full lane equality outside scope includes conditions, evidence and presentation.
        outside+=int(key not in allowed_keys and a!=b)
        authorized+=sum(bool(r.get('buy_authorized')) for r in b.get('tracking_candidates',[]))
        details.append(dict(lane=key,added=sorted(bb-aa),removed=sorted(aa-bb),rank_changed=rankchanges,
            baseline_top=[r['symbol'] for r in ranked(a)[:5]],candidate_top=[r['symbol'] for r in ranked(b)[:5]]))
    metrics=dict(eligibility_changed_count=changed,out_of_scope_changed_count=outside,buy_authorized_count=authorized,
        baseline_candidate_count=before,candidate_candidate_count=after,rank_changed_count=ranks,
        scan_failure_count=int(baseline.get('status')!='completed')+int(candidate.get('status')!='completed'))
    return metrics,details


def _write_new(path, data):
    raw=json.dumps(data,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False).encode('utf-8')
    with Path(path).open('xb') as file: file.write(raw)
    return hashlib.sha256(raw).hexdigest()


def run_experiment(database, item_id, input_path, output_dir):
    item=get_issue(database,item_id)
    require(item['state'] in ('experiment','observing'), 'Item must have an approved frozen experiment')
    require(item['current_experiment'] is not None, 'Missing frozen experiment')
    spec=item['experiments'][item['current_experiment']]['spec']
    require(spec.get('change_kind','ranking_config')=='ranking_config', 'Code experiments are not supported by this runner; cannot fabricate evidence or ready status')
    require(spec.get('validation_kind')=='engineering', 'This runner measures engineering invariants only, not profitability')
    require(spec.get('runner_hash')==runner_fingerprint(), 'Measurement runner changed since experiment design')
    from .source_archive import verify_source_archive
    verify_source_archive(spec.get('source_archive'),spec['baseline_code_hash'],spec['runner_hash'])
    require(all(c['metric'] in METRICS for c in spec['criteria']), 'Unsupported metric; external verified runner required')
    frozen,input_hash=read_frozen(input_path)
    config,generation=_baseline(database)
    fingerprint=code_fingerprint()
    require(input_hash==spec['input_hash'],'Frozen input bytes changed')
    require(fingerprint==spec['baseline_code_hash']==spec['candidate_code_hash'],'Executing code differs from frozen experiment')
    require(generation==spec['baseline_generation'] and digest(config)==spec['baseline_config_hash'],'Baseline configuration changed')
    require(spec['data_start']<=frozen['date']<=spec['data_end'],'Snapshot outside frozen experiment window')
    check_config_scope(spec,config)
    calendar=_calendar(database,frozen['date'])
    before=_run(frozen,config);after=_run(frozen,spec['candidate_config'])
    metrics,details=measure(before,after,set(spec['allowed_strategy_keys']))
    target=Path(output_dir).resolve()
    target.mkdir(parents=True,exist_ok=True)
    require(not any(target.iterdir()),'Experiment directory must be empty; refusing to replace previous evidence')
    baseline_hash=_write_new(target/'baseline.json',before)
    candidate_hash=_write_new(target/'candidate.json',after)
    measured=dict(input_hash=input_hash,baseline_code_hash=fingerprint,candidate_code_hash=fingerprint,
        holdout_id=spec['holdout_id'],sessions=[frozen['date']],observations=metrics['baseline_candidate_count'],metrics=metrics,
        method='deterministic same-code same-snapshot screen baseline/candidate ranking profiles',
        limitations='仅工程不变量与排序差异。一次快照=1独立交易日；候选有同日/行业相关性。没有成交模拟、收益验证或统计alpha证明。',
        validation_kind='engineering',runner_hash=runner_fingerprint(),calendar=calendar,details=details,
        artifacts={'baseline_sha256':baseline_hash,'candidate_sha256':candidate_hash},
        experiment_spec_hash=digest(spec),issue_id=item_id,issue_revision=item['revision'])
    measurement_hash=_write_new(target/'measurement.json',measured)
    evidence={**measured,'measurement_artifact':str(target/'measurement.json'),'measurement_hash':measurement_hash}
    _write_new(target/'evidence.json',evidence)
    receipt=dict(status='measured',comparison=compare(spec,evidence),evidence_path=str(target/'evidence.json'),
        input_hash=input_hash,live_effect='none',stage_advanced=False,notice='输出证据，尚需独立evaluator提交和validator复核；未启用配置。')
    _write_new(target/'receipt.json',receipt)
    return receipt
