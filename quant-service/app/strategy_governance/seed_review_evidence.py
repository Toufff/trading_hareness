"""Reconstruct bounded PV audit packets from explicit immutable releases and real captures."""
import hashlib
import json
from pathlib import Path
from .rules import require
from .review_evidence import write_packet,code_excerpt,compact_candidate


def _json(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def _sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


PLAN={
 'PV01':('accumulation','rules.py',['amount_multiple','fmean(a'], 'price_volume.py',['volume_unit','volume_multiple']),
 'PV02':('expansion','rules.py',['key == "expansion"','expansion_multiple','prior_high'], 'price_volume.py',['close_location','upper_wick_fraction']),
 'PV03':('pullback','rules.py',['pullback_multiple','prior_gain','drawdown'], 'price_volume.py',['def segment_evidence','pullback_amount_ratio','contracted =']),
 'PV04':('accumulation','rules.py',['accumulation','net5','net10'], 'price_volume.py',['down_up_amount_ratio','churn_max_change_pct','lane == \'accumulation\'']),
 'PV05':('contraction','conditions.py',['contraction','prior_high','prior10_high'], 'conditions.py',['contraction','prior10_high']),
 'PV06':('reclaim','advanced_strategies.py',['panic_break','reclaimed_prior_low','latest_change','previous_change'], 'advanced_strategies.py',['recovery_fraction','meaningful_recovery','reclaim_reference']),
 'PV07':('rotation','advanced_strategies.py',['rotation','latest_breadth','stable_leaders'], 'price_volume.py',['lane == \'relay\'','lane == \'rotation\'','limit_board_process']),
 'PV08':('accumulation','conditions.py',['def watch_conditions','confirmation','invalidation'], 'conditions.py',['price_volume','references','confirmation']),
}


def build_seed_packets(directory,old_release,new_release,*,root=None):
    directory=Path(directory);old_release=Path(old_release).resolve();new_release=Path(new_release).resolve()
    require('current' not in str(old_release).lower() and old_release!=new_release,'Explicit distinct immutable release roots required; never use current as the old version')
    paths={'frozen':directory/'frozen-input.json','baseline':directory/'baseline/2026-09-11_short_term_lanes.json',
           'candidate':directory/'candidate/2026-09-11_short_term_lanes.json'}
    frozen,baseline,candidate=(_json(paths[k]) for k in ('frozen','baseline','candidate'))
    source_hashes={k:_sha(path) for k,path in paths.items()}
    old_app=old_release/'app/quant-service/app/short_term_lanes';new_app=new_release/'app/quant-service/app/short_term_lanes'
    for release,app,result,label in [(old_release,old_app,baseline,'baseline'),(new_release,new_app,candidate,'candidate')]:
        source=(app/'__init__.py').read_text(encoding='utf-8-sig')
        require(result['version'] in source,f'{label} release version does not match captured result')
        manifest=release/'app/release-manifest.json'
        require(manifest.is_file(),'Explicit release manifest missing')
        source_hashes[label+'_release_manifest']=_sha(manifest)
    by_old={l['key']:l for l in baseline['lanes']};by_new={l['key']:l for l in candidate['lanes']}
    references={}
    for key,(lane,old_file,old_needles,new_file,new_needles) in PLAN.items():
        original={r['symbol']:r for r in by_old[lane].get('tracking_candidates',[])}
        current=by_new[lane].get('tracking_candidates',[])
        if key in ('PV03','PV04','PV06'):
            current=[r for r in current if r.get('price_volume',{}).get('status')=='quality_warning']
        elif key=='PV02':
            current=[r for r in current if (r.get('price_volume',{}).get('metrics',{}).get('close_location') or 1)<.45
                and (r.get('price_volume',{}).get('metrics',{}).get('upper_wick_fraction') or 0)>=.45]
        cases=[]
        for row in current[:2]:
            symbol=row['symbol']
            raw=[{k:r.get(k) for k in ('symbol','trade_date','close','amount','main_net','pct_chg','turnover_rate','flow_convention')}
                for r in frozen['rows'] if r['symbol']==symbol][-11:]
            ohlc=[{k:r.get(k) for k in ('date','open','high','low','close','amount','volume','volume_unit','source')}
                for r in frozen['price_histories'].get(symbol,[])][-3:]
            cases.append({'kind':'real_captured_stock','lane':lane,'symbol':symbol,'raw_flow_rows':raw,'raw_ohlc':ohlc,
                'before':compact_candidate(original.get(symbol,{})),'after':compact_candidate(row)})
        ready=bool(cases) and key in ('PV03','PV04','PV06','PV08')
        reasons={
            'PV01':'已有成交额/成交股数口径源码与样本，但本轮未证明旧输出实际错误地把额当作股数；缺少该命题的原始反例。',
            'PV02':'本次真实启动样本没有同时低收盘位置及长上影的反例；只能看出旧代码未消费这些字段，不能伪造当日误选。',
            'PV05':'本轮收缩突破无匹配候选，缺少筛选参考线与报告参考线不一致的实际输出案例。',
            'PV07':'本轮无接力匹配，轮动数据不能证明回封质量；该事项含两类边界，需拆分或补齐对应原始案例。',
        }
        code=[{'version':'baseline',**code_excerpt(old_app/old_file,old_needles,maximum=45)},
              {'version':'candidate',**code_excerpt(new_app/new_file,new_needles,maximum=60)}]
        references[key]=write_packet(issue_key='20260911:user-price-volume:'+key,question='独立核对该原子量价改进是否有原始证据支持，区分代码缺陷与收益假设。',
            cases=cases,code=code,facts={'date':frozen['date'],'baseline_version':baseline['version'],'candidate_version':candidate['version'],
                'actual_case_count':len(cases),'market_counterexample_or_contract_sample_available':ready},
            source_hashes=source_hashes,status='ready' if ready else 'missing',reason=None if ready else reasons[key],
            limitations=['本轮单日真实数据，不含未来收益。','历史讨论6.15%跌后0.11%反弹为构造测试，不是这里的真实股票样本。',
                '源码明确取自固定旧release，不使用current别名；摘录不是完整模块。','系统只提供事实供质疑，尚未自行批准问题成立。'],root=root)
    return references
