"""Prepare original code/data excerpts for independent review, not a claimed verdict."""
import ast
import argparse
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=Path('G:/StockPlatform/data/research/2026-09-11-governance-pv-acceptance')


def functions(path,names):
    source=path.read_text(encoding='utf-8'); rows=source.splitlines()
    return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'functions':{n.name:'\n'.join(rows[n.lineno-1:n.end_lineno]) for n in ast.parse(source).body
                     if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in names}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-release',type=Path,required=True)
    parser.add_argument('--new-release',type=Path,required=True)
    args=parser.parse_args()
    if 'current' in str(args.old_release).lower() or args.old_release.resolve()==args.new_release.resolve():
        parser.error('Explicit distinct immutable releases required; current cannot identify historical code')
    old_app=args.old_release/'app/quant-service/app/short_term_lanes'
    new_app=args.new_release/'app/quant-service/app/short_term_lanes'
    baseline=json.loads((EVIDENCE/'baseline/2026-09-11_short_term_lanes.json').read_text(encoding='utf-8'))
    candidate=json.loads((EVIDENCE/'candidate/2026-09-11_short_term_lanes.json').read_text(encoding='utf-8'))
    for app,result in [(old_app,baseline),(new_app,candidate)]:
        if result['version'] not in (app/'__init__.py').read_text(encoding='utf-8-sig'):
            raise ValueError('Explicit release version does not match captured result')
    seed=json.loads((EVIDENCE/'seed-receipt.json').read_text(encoding='utf-8'))
    issue=next(x['id'] for x in seed if x['change_id']=='PV06')
    def rows(result):
        return {(lane['key'],r['symbol']):r for lane in result['lanes'] for r in lane['selected']+lane.get('caution_list',[])}
    old,new=rows(baseline),rows(candidate)
    examples=[]
    for (lane,symbol),r in new.items():
        if lane != 'reclaim' or (lane,symbol) not in old:continue
        if r.get('price_volume',{}).get('status')!='quality_warning':continue
        examples.append({'lane':lane,'symbol':symbol,'name':r['name'],
            'old':{k:old[lane,symbol].get(k) for k in ('reason','caution','state','price_volume')},
            'new':{k:r.get(k) for k in ('reason','caution','state','price_volume')}})
        if len(examples)==3:break
    packet={issue:{'baseline_version':baseline['version'],'candidate_version':candidate['version'],
        'same_input_sha256':json.loads((EVIDENCE/'candidate-receipt.json').read_text(encoding='utf-8'))['input_hash'],
        'old_rule_excerpt':functions(old_app/'rules.py',{'screen'}),
        'old_advanced_rule':functions(old_app/'advanced_strategies.py',{'evaluate','ohlc_features'}),
        'new_advanced_rule':functions(new_app/'advanced_strategies.py',{'evaluate','ohlc_features'}),
        'current_rule_excerpt':functions(new_app/'price_volume.py',{'PriceVolumeSettings','daily_bar','assess'}),
        'same_input_observations':examples,
        'limitations':'同日收盘对照，不含未来收益；旧规则若未选中函数不能据此推断该函数不存在。复核者不得将当前修复结果视为盈利验证。'}}
    if not examples:raise ValueError('No actual comparable examples; do not spend model tokens on an empty packet')
    raw=json.dumps(packet,ensure_ascii=False,indent=2).encode('utf-8')
    path=EVIDENCE/('review-packet-pv06-'+hashlib.sha256(raw).hexdigest()[:16]+'.json')
    if not path.exists():
        with path.open('xb') as file:file.write(raw)
    print(json.dumps({'path':str(path),'bytes':path.stat().st_size,'examples':len(examples)}))


if __name__=='__main__':main()
