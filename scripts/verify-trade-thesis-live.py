"""Read-only deployed acceptance: owner, adapter and public projection must agree."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests


def verify(symbol, public_base):
    session = requests.Session()
    session.trust_env = False
    results = []
    identity = None
    for label, url in (
        ('owner', 'http://127.0.0.1:5681/api/v1/research/theses'),
        ('adapter', 'http://127.0.0.1:5680/api/research/theses'),
        ('public', public_base.rstrip('/') + '/api/research/theses'),
    ):
        response = session.get(url, params={'symbol': symbol}, timeout=30)
        response.raise_for_status()
        body = response.json()
        assert body.get('live_effect') == 'none', f'{label}: unexpected live effect'
        items = body['items']
        assert items, f'{label}: no stored thesis for {symbol}'
        current = sorted((i['thesis']['thesis_id'], i['evaluation']['evaluation_id'],
                          i['evaluation']['content_hash']) for i in items)
        if identity is None:
            identity = current
        assert current == identity, f'{label}: different evaluation or hash'
        result = items[0]['evaluation']
        assert result['source_run_id'] and result['observations'], f'{label}: missing real evidence'
        assert result.get('decision_binding') is False, f'{label}: unexpected decision binding'
        timeline = session.get(url + '/' + items[0]['thesis']['thesis_id'] + '/timeline', timeout=30)
        timeline.raise_for_status()
        assert any(e['evaluation_id'] == result['evaluation_id'] for e in timeline.json()['evaluations'])
        before = session.get(url, params={'symbol': symbol, 'as_of': '2000-01-01T00:00:00Z'}, timeout=30)
        before.raise_for_status()
        assert before.json()['items'] == [], f'{label}: future record leaked into historical view'
        results.append({'surface': label, 'status': response.status_code, 'historical_filter': True,
                        'evaluation_id': result['evaluation_id'], 'content_hash': result['content_hash'],
                        'source_run_id': result['source_run_id'], 'cutoff_at': result['cutoff_at'],
                        'data_date': result['data_date'], 'observations': len(result['observations'])})
    return {'status': 'passed', 'symbol': symbol, 'decision_binding': False, 'checks': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--public-base', default='https://stock.toufai.top')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    receipt = verify(args.symbol, args.public_base)
    text = json.dumps(receipt, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
