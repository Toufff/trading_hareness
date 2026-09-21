"""Bounded fill-only repair of known follow-up gaps, using licensed Longhu only.

No source candidate, recommendation, discipline plan or strategy is rewritten.
Original evidence and a full receipt are archived; --apply is explicit.
"""
import argparse
from datetime import date
import importlib.util
import json
from pathlib import Path
from uuid import uuid4


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date',required=True,type=date.fromisoformat)
    p.add_argument('--evidence-dir',required=True)
    p.add_argument('--env-file',default='G:/StockPlatform/config/runtime.env')
    p.add_argument('--apply',action='store_true')
    args=p.parse_args()
    spec=importlib.util.spec_from_file_location('gap_cli',Path(__file__).with_name('daily-bar-gap-backfill.py'))
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    cli.load_env_file(args.env_file);cli.clear_proxies()
    from app.daily_bar_gap_backfill import EvidenceStore,build_plan,persist_session
    from app.longhu_vendor_source import intraday_source
    from app.system_business_acceptance import INDICES
    with cli.connection(read_only=True) as c:
        gaps=c.execute("""WITH q AS (SELECT DISTINCT ON(origin_id) evidence
          FROM quant.strategy_observation_evaluations WHERE as_of_date=%s
          ORDER BY origin_id,recorded_at DESC)
          SELECT DISTINCT evidence->>'symbol' AS symbol,
            jsonb_array_elements_text(evidence->'missing_dates')::date AS day
          FROM q WHERE evidence->>'status'='data_gap'""",(args.date,)).fetchall()
        present={r['symbol'] for r in c.execute('SELECT symbol FROM quant.canonical_bars_daily WHERE trading_date=%s AND symbol=ANY(%s)',(args.date,list(INDICES))).fetchall()}
    target={(r['symbol'],r['day']) for r in gaps}
    target.update((s,args.date) for s in INDICES if s not in present)
    symbols=sorted({s for s,d in target})
    if len(symbols)>300:
        raise ValueError('Bounded maintenance allows 300 affected stocks; split the incident first')
    root=Path(args.evidence_dir);store=EvidenceStore(root)
    fetched=cli.fetch_evidence(store,intraday_source(),[(s,d.strftime('%Y%m%d')) for s,d in sorted(target) if s not in INDICES],symbols,4)
    with cli.connection(read_only=True) as c:
        plan=build_plan(c,store,min((d for s,d in target),default=args.date),args.date)
    receipts=[];selected=[]
    for day,bars in sorted(plan.rows.items()):
        bars=[b for b in bars if (b.symbol,day) in target]
        selected.extend({'symbol':b.symbol,'date':str(day)} for b in bars)
        if args.apply and bars:
            with cli.connection(read_only=False) as c:
                receipts.append(persist_session(c,day,bars,plan.evidence,run_id='followup-repair-'+uuid4().hex))
    unresolved=len(target)-len(selected)
    result={'status':'partial' if unresolved else 'completed','unresolved_pairs':unresolved,
            'requested':[{'symbol':s,'date':str(d)} for s,d in sorted(target)],
            'fetch':fetched,'eligible':selected,'held':plan.held,'flags':plan.flags,
            'applied':args.apply,'receipts':receipts,'original_candidates_changed':False}
    cli.emit(result,str(root/'receipt.json'))
    return 1 if unresolved else 0


if __name__=='__main__':raise SystemExit(main())
