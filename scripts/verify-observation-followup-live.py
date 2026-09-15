"""Actual G-drive ledger/backfill acceptance, no broker operations or orders."""
import argparse,json,os,sys
from pathlib import Path
from datetime import date,datetime,timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'quant-service'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date',type=date.fromisoformat,required=True)
    p.add_argument('--env-file',default='G:/StockPlatform/config/runtime.env')
    p.add_argument('--import-report',action='append',default=[])
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--migrate',action='store_true')
    args=p.parse_args();sys.stdout.reconfigure(encoding='utf-8')
    for l in Path(args.env_file).read_text(encoding='utf-8-sig').splitlines():
        if '=' in l and not l.startswith('#'):
            k,v=l.split('=',1);os.environ[k]=v
    if args.migrate:
        from alembic import command
        from alembic.config import Config
        os.chdir(ROOT/'quant-service');command.upgrade(Config('alembic.ini'),'head')
    from app.database import Database
    from app.short_term_lanes.tracking_rules import origins,digest
    from app.short_term_lanes.tracking_repository import persist_origins,refresh
    from app.short_term_lanes.repository import load
    from app.short_term_lanes.rules import screen,Settings
    from app.short_term_lanes.reports import make_bundle,write_bundle
    from app.ranking_factors import FactorSpec
    db=Database();imports=[]
    for path in args.import_report:
        r=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if r['as_of_date']>=str(args.date):raise ValueError('Historical report must predate evaluation')
        source='historical_report:'+Path(path).name+':'+digest(r)
        n=persist_origins(db,origins(r,datetime.now(timezone.utc).isoformat(),source))
        imports.append(dict(path=path,records=n,source=source))
    rows,sessions=load(db,args.date)
    # All strategies use stored strict OHLC for this acceptance; no synthetic bars.
    with db.transaction() as c:
        bars=c.execute('''SELECT symbol,trading_date,open,high,low,close,volume,amount
            FROM quant.canonical_bars_daily WHERE trading_date BETWEEN %s::date-160 AND %s
            AND symbol IN ('001232.SZ','603366.SH','600360.SH') ORDER BY symbol,trading_date''',(args.date,args.date)).fetchall()
    today=screen(rows,sessions,str(args.date),settings=Settings(ranking_factors=(FactorSpec('small_cap'),)))
    # This diagnostic does not publish a deficient OHLC scan as the live nine-strategy result.
    today['followup']=refresh(db,today,args.date)
    once=today['followup'];twice=refresh(db,today,args.date)
    targets={s:[e for e in twice['items'] if e['symbol']==s and e['signal_date']=='2026-09-10'] for s in ['001232.SZ','603366.SH']}
    with db.transaction() as c:
        count=c.execute('SELECT count(*) AS n FROM quant.strategy_observation_evaluations').fetchone()['n']
    refresh(db,today,args.date)
    with db.transaction() as c:
        count2=c.execute('SELECT count(*) AS n FROM quant.strategy_observation_evaluations').fetchone()['n']
    checks=dict(both_targets=all(targets.values()),calendar_complete=twice.get('calendar_complete') is True,
        no_duplicate_evaluations=count==count2,repeated_read_equal=once==twice,
        target_day1_observed=all(e['windows']['1']['status']=='observed' for rs in targets.values() for e in rs),
        future_not_fabricated=all(e['windows']['3']['status']=='not_due' for rs in targets.values() for e in rs),
        not_trade_returns=all(e['trade_return_pct'] is None for rs in targets.values() for e in rs))
    today['report_bundle']=make_bundle(today);args.output_dir.mkdir(parents=True,exist_ok=True)
    write_bundle(args.output_dir,today,today['report_bundle'])
    receipt=dict(passed=all(checks.values()),checks=checks,imports=imports,targets=targets,bars=bars,
        scope='real G DB observation ledger; diagnostic stored-bar scan is not full production publication')
    (args.output_dir/'acceptance.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({'passed':receipt['passed'],'checks':checks,'targets':{s:[{'return':e['windows']['1'],'path':e['path_check'],'series':e['series']} for e in v[:1]] for s,v in targets.items()}},ensure_ascii=False))
    raise SystemExit(0 if receipt['passed'] else 1)


if __name__=='__main__':main()
