"""Deployed acceptance; writes only with explicit --verify-write-idempotency."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests


def verify(symbol, public_base, credentials_file=None, verify_write=False,
           env_file='G:/StockPlatform/config/runtime.env'):
    session = requests.Session()
    session.trust_env = False
    public = requests.Session()
    public.trust_env = False
    if credentials_file and Path(credentials_file).is_file():
        credentials = json.loads(Path(credentials_file).read_text(encoding='utf-8'))
        public.auth = (credentials['username'], credentials['password'])
    results = []
    identity = None
    for label, url in (
        ('owner', 'http://127.0.0.1:5681/api/v1/research/theses'),
        ('adapter', 'http://127.0.0.1:5680/api/research/theses'),
        ('public', public_base.rstrip('/') + '/api/research/theses'),
    ):
        client = public if label == 'public' else session
        response = client.get(url, params={'symbol': symbol}, timeout=30)
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
        timeline = client.get(url + '/' + items[0]['thesis']['thesis_id'] + '/timeline', timeout=30)
        timeline.raise_for_status()
        assert any(e['evaluation_id'] == result['evaluation_id'] for e in timeline.json()['evaluations'])
        before = client.get(url, params={'symbol': symbol, 'as_of': '2000-01-01T00:00:00Z'}, timeout=30)
        before.raise_for_status()
        assert before.json()['items'] == [], f'{label}: future record leaked into historical view'
        results.append({'surface': label, 'status': response.status_code, 'historical_filter': True,
                        'evaluation_id': result['evaluation_id'], 'content_hash': result['content_hash'],
                        'source_run_id': result['source_run_id'], 'cutoff_at': result['cutoff_at'],
                        'data_date': result['data_date'], 'observations': len(result['observations'])})
    if verify_write:
        from dotenv import dotenv_values
        key = dotenv_values(env_file).get('QUANT_WRITE_API_KEY')
        assert key, 'Missing owner write credential'
        before = results[0]
        response = session.post('http://127.0.0.1:5681/api/v1/research/theses/evaluate',
            headers={'X-Quant-Write-Key': key}, json={
                'source_run_id': before['source_run_id'], 'cutoff_at': before['cutoff_at'],
                'symbols': [symbol], 'namespace': 'shadow'}, timeout=45)
        response.raise_for_status()
        receipt = response.json()
        assert receipt['status'] == 'completed' and receipt['failed'] == 0, 'Evaluation failed'
        assert receipt['items'][0]['status'] == 'idempotent', 'Repeated evaluation was not idempotent'
        after = session.get('http://127.0.0.1:5681/api/v1/research/theses',
                            params={'symbol': symbol}, timeout=30).json()['items'][0]['evaluation']
        assert after['evaluation_id'] == before['evaluation_id'], 'Repeated write changed evaluation'
        assert after['content_hash'] == before['content_hash'], 'Repeated write changed evidence'
        results.append({'surface': 'owner_write', 'status': response.status_code,
                        'idempotent': True, 'evaluation_id': after['evaluation_id']})
    return {'status': 'passed', 'symbol': symbol, 'decision_binding': False, 'checks': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--public-base', default='https://stock.toufai.top')
    parser.add_argument('--credentials-file', default='C:/Users/brave/.stockbrain/dashboard-credentials.json')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--verify-write-idempotency', action='store_true')
    parser.add_argument('--env-file', default='G:/StockPlatform/config/runtime.env')
    args = parser.parse_args()
    receipt = verify(args.symbol, args.public_base, args.credentials_file,
                     args.verify_write_idempotency, args.env_file)
    text = json.dumps(receipt, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
