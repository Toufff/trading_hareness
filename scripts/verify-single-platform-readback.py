"""Read-only production/public consistency check, not trading-plan acceptance."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import requests


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def main():
    session = requests.Session()
    session.trust_env = False
    credentials = json.loads(Path(r'C:\Users\brave\.stockbrain\dashboard-credentials.json').read_text())
    public = requests.Session()
    public.trust_env = False
    public.auth = (credentials['username'], credentials['password'])
    if credentials.get('magic_cookie_token'):
        public.cookies.set('stockbrain_access', credentials['magic_cookie_token'], domain='stock.toufai.top')
    results = []
    for day in ('2026-09-07', '2026-09-08', '2026-09-09'):
        rows = []
        for client, base, endpoint in (
            (session, 'http://127.0.0.1:5681', '/api/v1/strategy/post-close/latest'),
            (session, 'http://127.0.0.1:5680', '/api/research/strategy/post-close/latest'),
            (public, 'https://stock.toufai.top', '/api/v1/strategy/post-close/latest'),
        ):
            print(json.dumps({'checking': base, 'date': day}), flush=True)
            response = client.get(base + endpoint, params={'as_of_date': day}, timeout=45)
            response.raise_for_status()
            run = response.json()['run']
            rows.append({'run_id': run['run_id'], 'date': run['as_of_date'],
                         'sha256': digest(run['summary']['strategy_lanes'])})
        file = Path(r'G:\StockPlatform\reports\short-term') / f'{day}_short_term_lanes.json'
        file_hash = digest(json.loads(file.read_text(encoding='utf-8')))
        results.append({'date': day, 'passed': rows[0] == rows[1] == rows[2]
                        and rows[0]['date'] == day and rows[0]['sha256'] == file_hash,
                        'projections': rows, 'report_sha256': file_hash})
    receipt = {'checked_at': datetime.now(timezone.utc).isoformat(),
               'scope': 'same dated strategy result across owner, adapter, public API and disk; not research completeness',
               'passed': all(r['passed'] for r in results), 'results': results}
    output = Path(r'G:\StockPlatform\reports\single-platform-readback-20260910.json')
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    raise SystemExit(0 if receipt['passed'] else 1)


if __name__ == '__main__':
    main()
