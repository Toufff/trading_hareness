"""Stored-run thesis tracking CLI. No broker, no rescoring, no live decisions."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'quant-service'))


def main():
    from dotenv import load_dotenv
    from app.database import Database
    from app.trade_thesis.repository import list_latest
    from app.trade_thesis.scan_adapter import evaluate_source_run, refresh_from_run, load_source
    from app.trade_thesis.report import sections
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['evaluate', 'show', 'attach'])
    parser.add_argument('--env-file', default='G:/StockPlatform/config/runtime.env')
    parser.add_argument('--source-run-id')
    parser.add_argument('--symbol', action='append')
    parser.add_argument('--cutoff-at')
    parser.add_argument('--namespace', choices=['shadow', 'advisory'], default='shadow')
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    load_dotenv(args.env_file, override=False)
    db = Database()
    if args.command == 'show':
        with db.transaction() as c:
            c.execute('SET TRANSACTION READ ONLY')
            result = dict(status='ok', items=list_latest(c, symbol=(args.symbol or [None])[0], namespace=args.namespace))
    elif args.command == 'attach':
        if args.symbol or args.cutoff_at or args.namespace != 'shadow':
            parser.error('attach is the automatic whole-run shadow path; use evaluate for filtered runs')
        source = load_source(db, args.source_run_id)
        result = refresh_from_run(db, source['run_id'], source['kind'])
    else:
        result = evaluate_source_run(db, args.source_run_id, args.cutoff_at, args.symbol, args.namespace)
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        name = 'thesis-' + (result.get('source_run_id') or 'latest')
        (args.output_dir / (name + '.json')).write_text(json.dumps(result, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
        if args.command != 'show':
            (args.output_dir / (name + '.md')).write_text('\n'.join(sections(result)), encoding='utf-8')
    db.close()
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result['status'] in ('completed', 'ok') else 2


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    raise SystemExit(main())
