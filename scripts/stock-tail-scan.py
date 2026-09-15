"""Re-evaluate a captured late-session input with formal strategy rules."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'quant-service'))
from app.intraday_scan import tail
from app.intraday_scan.rules import digest

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--input')
    g.add_argument('--replay',help='Frozen tail input; no DB/network access')
    p.add_argument('--output-root',default='G:/StockPlatform/reports/tail')
    a=p.parse_args()
    if a.replay:
        frozen=json.loads(Path(a.replay).read_text(encoding='utf-8'))
        if frozen['implementation_hash']!=tail.implementation_hash():raise ValueError('Replay needs original release')
        result=tail.build(frozen['data'],settings=tail.restore_settings(frozen['settings']),events=frozen['events'])
        if digest(result)!=frozen['result_hash']:raise ValueError('Replay result mismatch')
        print(json.dumps(dict(replay=True,result_hash=digest(result),cutoff=result['cutoff'])));return
    data=json.loads(Path(a.input).read_text(encoding='utf-8'))
    from dotenv import load_dotenv
    load_dotenv('G:/StockPlatform/config/runtime.env',override=True)
    from app.database import Database
    from app.short_term_lanes.service import governed_selection
    from app.short_term_lanes.repository import verified_events
    db=Database()
    try:
        settings,governance=governed_selection(db)
        events=verified_events(db,__import__('datetime').date.fromisoformat(data['cutoff'][:10]))
    finally:db.close()
    result=tail.build(data,settings=settings,events=events)
    if digest(result)!=digest(tail.build(data,settings=settings,events=events)):
        raise RuntimeError('Tail replay mismatch')
    result_hash=digest(result)
    result['governance']=governance
    directory=Path(a.output_root)/(data['cutoff'][:10]+'-'+data['cutoff'][11:16].replace(':','')+'-'+digest(data)[:8])
    if directory.exists():raise ValueError('Refusing to overwrite a frozen tail run')
    report=tail.write(result,directory)
    (directory/'frozen-input.json').write_text(json.dumps(dict(data=data,events=events,settings=__import__('dataclasses').asdict(settings),implementation_hash=tail.implementation_hash(),result_hash=result_hash),ensure_ascii=False,default=str),encoding='utf-8')
    print(json.dumps(dict(report=report,cutoff=data['cutoff'],coverage=result['coverage'],
                         lanes=[dict(key=l['key'],matches=l['total_matches'],status=l['status']) for l in result['lanes']]),ensure_ascii=False))

if __name__=='__main__':main()
