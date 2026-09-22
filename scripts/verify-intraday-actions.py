"""Read-only owner coverage / isolated JSON-context replay; never sends Feishu.

Examples: --read-only-owner --env-file G:/StockPlatform/config/runtime.env
          --contexts fixture.json --state shadow.sqlite3
Synthetic contexts are labelled replay, not real-time acceptance. No deployment.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'quant-service'))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--read-only-owner', action='store_true')
    mode.add_argument('--contexts', type=Path)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.read_only_owner:
        if args.env_file is None:
            parser.error('--read-only-owner requires --env-file')
        import psycopg
        from psycopg.rows import dict_row
        from dotenv import dotenv_values
        from app.db_dsn import connection_params
        from app.intraday_actions.service import load_readonly_source, assemble_contexts, ActionShadowService
        from app.intraday_actions.repository import ShadowRepository
        params = connection_params(dotenv_values(args.env_file))
        with psycopg.connect(**params, row_factory=dict_row, connect_timeout=10) as connection:
            connection.execute('SET TRANSACTION READ ONLY')
            connection.execute("SET LOCAL statement_timeout='15s'")
            source = load_readonly_source(connection, account_key='citics-primary', as_of=datetime.now(timezone.utc))
        shadow = ShadowRepository(':memory:')
        try:
            contexts = assemble_contexts(source, quotes={}, minutes={}, markets={}, costs={}, policy={}, compiler_policy={}, repository=shadow)
            evaluated = ActionShadowService(shadow).evaluate_batch(contexts)
        finally:
            shadow.close()
        items = []
        for item in source['items']:
            plans = [p for p in source['discipline_plans'] if p['symbol'] == item['symbol']]
            applicable = [p for p in plans if p['plan_kind'] == ('holding' if item['role'] == 'holding' else 'new_buy')]
            def bound(p):
                if item['role'] == 'holding':
                    return str((p.get('position') or {}).get('snapshot_id')) == str(source['snapshot_id'])
                return 'recommendation_decision:' + str(source['decision_id']) in (p.get('evidence_refs') or [])
            bases = sorted({str((line.get('confirm') or {}).get('basis'))
                            for p in applicable if bound(p) for line in (p.get('lines') or [])})
            items.append({'symbol': item['symbol'], 'name': item['name'], 'role': item['role'],
                          'plans': [{'id': str(p['plan_id']), 'kind': p['plan_kind'], 'status': p['status'], 'created_at': p['created_at'],
                                     'matches_current_role': p in applicable, 'matches_scope_evidence': bound(p)} for p in plans],
                          'existing_confirmation_bases': bases,
                          'six_action_runtime_connected': False})
        receipt = {'mode': 'actual_owner_read_only_inventory', 'as_of': source['as_of'],
                   'production_writes': False, 'sent_messages': 0, 'items': items,
                   'scope_blockers': source['scope_blockers'], 'deployment_accepted': False,
                   'shadow_coverage': [{'symbol': item['symbol'], 'status': item['status'],
                                        'coverage': item['coverage'], 'assembly_blockers': item['assembly_blockers']} for item in evaluated],
                   'missing_acceptance': ['minute_policy_promotion', 'verified_fill_reconciliation',
                       'production_transactional_state_adapter', 'live_intraday_shadow_acceptance']}
    else:
        if args.state is None:
            parser.error('--contexts requires --state (isolated SQLite path)')
        from app.intraday_actions.repository import ShadowRepository
        from app.intraday_actions.service import ActionShadowService
        contexts = json.loads(args.contexts.read_text(encoding='utf-8-sig'))
        if not isinstance(contexts, list):
            parser.error('contexts must be an ordered list of point-in-time contexts')
        repo = ShadowRepository(args.state)
        try:
            results = ActionShadowService(repo).evaluate_batch(contexts)
            receipt = {'mode': 'isolated_context_replay', 'sent_messages': 0, 'production_writes': False,
                       'results': results, 'pending_count': len(repo.pending()), 'deployment_accepted': False}
        finally:
            repo.close()
    text = json.dumps(receipt, ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + '\n', encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
