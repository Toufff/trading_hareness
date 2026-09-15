"""Read-only real-data regression. Prints bounded evidence, never credentials."""
import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'quant-service'))


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', required=True)
    parser.add_argument('--env-file', default='G:/StockPlatform/config/runtime.env')
    args = parser.parse_args()
    for line in Path(args.env_file).read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            os.environ[key.strip()] = value.strip().strip('"').strip("'")
    os.environ['PGOPTIONS'] = '-c default_transaction_read_only=on -c statement_timeout=30000'
    from app.database import Database
    from app.short_term_lanes.repository import load
    from app.short_term_lanes.rules import screen, Settings, features, mainboard
    from app.short_term_lanes.flow_experiments import compare
    rows, sessions = load(Database(), date.fromisoformat(args.date))
    settings = Settings()
    result = screen(rows, sessions, args.date, settings=settings)
    hits = []
    for lane in result['lanes']:
        entries = {r['symbol']:r for r in lane['tracking_candidates']}
        if '000636.SZ' in entries:
            row = entries['000636.SZ']
            hits.append(dict(lane=lane['key'], state=row['state'], execution=row['execution'],
                discovery_score=row['discovery_score'], displayed=any(r['symbol']=='000636.SZ' for r in lane['observation_list'])))
    checks = dict(complete=result['status']=='completed',
        fenghua_expansion=any(r['lane']=='expansion' for r in hits),
        fenghua_relay=any(r['lane']=='relay' for r in hits),
        no_trade_authorization=all(not r['buy_authorized'] for l in result['lanes'] for r in l['tracking_candidates']))
    experiment = compare(rows, sessions, settings, features, mainboard)
    print(json.dumps(dict(checks=checks, version=result['version'], sessions=sessions,
        coverage=result['coverage'], fenghua=hits,
        lanes=[dict(key=l['key'], matches=l['total_matches'], observation=[r['name'] for r in l['observation_list']]) for l in result['lanes']],
        experiment=experiment), ensure_ascii=False, default=str))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
