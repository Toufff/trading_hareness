"""Read-only real-data on/off replay. Never replace official reports or runs."""
import argparse
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'quant-service'))
import psycopg
from psycopg.rows import dict_row
from app.db_dsn import connection_params
from app.short_term_lanes.service import build, configured_settings
from app.short_term_lanes.rules import LANES
from app.short_term_lanes.candidate_history import fetch
from app.short_term_lanes.reports import write_bundle
from app.ranking_factors import FactorSpec


class ReadOnlyDatabase:
    @contextmanager
    def transaction(self):
        with psycopg.connect(**connection_params(), row_factory=dict_row,
                options='-c default_transaction_read_only=on -c statement_timeout=30000') as connection:
            yield connection


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date',required=True,type=date.fromisoformat)
    p.add_argument('--env-file',default='G:/StockPlatform/config/runtime.env')
    p.add_argument('--output-dir',required=True,type=Path)
    args=p.parse_args()
    official=Path('G:/StockPlatform/reports/short-term').resolve()
    if args.output_dir.resolve()==official:
        p.error('Use a separate evidence directory')
    env=dict(l.split('=',1) for l in Path(args.env_file).read_text(encoding='utf-8-sig').splitlines() if '=' in l and not l.startswith('#'))
    os.environ.update(env)
    cache={}
    def history(symbols,day):
        if not cache: cache['value']=fetch(symbols,day)
        return cache['value']
    db=ReadOnlyDatabase();settings=configured_settings(disabled=True)
    base=build(db,args.date,settings=settings,history_fetcher=history,tracking_write=False)
    print(json.dumps({'stage':'baseline','status':base['status'],'coverage':base['coverage']},ensure_ascii=False),flush=True)
    checks={'baseline_ready':base['status']=='completed','nine_strategies':len(base['lanes'])==9}
    variants={}
    for scope in [('accumulation','pullback'), ('*',)]:
        key='selected-lanes' if scope!=('*',) else 'all-lanes'
        variant=build(db,args.date,settings=replace(settings,ranking_factors=(FactorSpec('small_cap',strategies=scope),)),history_fetcher=history,tracking_write=False)
        changes=[]
        for before,after in zip(base['lanes'],variant['lanes']):
            targeted=scope==('*',) or before['key'] in scope
            checks[f'{key}:{before["key"]}:match_count']=before['total_matches']==after['total_matches']
            if not targeted:checks[f'{key}:{before["key"]}:unchanged']=before==after
            policy=after.get('factor_policy')
            if policy and before['total_matches']:
                checks[f'{key}:{before["key"]}:cap_coverage']=all(f['context']['status']=='ready' for f in policy['factors'])
            for row in after['selected']:
                checks[f'{key}:{before["key"]}:{row["symbol"]}:safe']=row['state']=='watch' and row['buy_authorized'] is False
            changes.append({'strategy':before['label'],'total_matches':after['total_matches'],
                'before':[r['name'] for r in before['selected']],
                'after':[{'name':r['name'],'symbol':r['symbol'],'factor_overlay':r.get('factor_overlay')} for r in after['selected']],
                'policy':policy})
        write_bundle(args.output_dir/key,variant,variant['report_bundle'])
        reread=json.loads((args.output_dir/key/f'{args.date}_short_term_lanes.json').read_text(encoding='utf-8'))
        checks[f'{key}:report_roundtrip']=reread['report_bundle']==variant['report_bundle']
        variants[key]=changes
    off_again=build(db,args.date,settings=configured_settings(disabled=True),history_fetcher=history,tracking_write=False)
    checks['off_restores_baseline']=off_again==base
    receipt={'as_of_date':str(args.date),'passed':all(checks.values()),'checks':checks,
             'scope':'real production DB inputs, read-only replay; not a profitability or scheduler validation',
             'variants':variants}
    args.output_dir.mkdir(parents=True,exist_ok=True)
    (args.output_dir/'acceptance.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({'passed':receipt['passed'],'checks':checks,'receipt':str(args.output_dir/'acceptance.json')},ensure_ascii=False))
    raise SystemExit(0 if receipt['passed'] else 1)


if __name__=='__main__':main()
