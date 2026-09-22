"""Release decision is separate from engine correctness or service liveness."""
REQUIRED = (
    'engine_regression', 'compiler_regression', 'transaction_restart_regression',
    'same_round_scope_coverage', 'point_in_time_history_replay',
    'real_intraday_shadow', 'production_store_adapter', 'live_runtime_wiring',
    'readable_six_action_cards', 'backend_full_suite', 'frontend_checks',
    'adapter_checks', 'independent_review',
)


def release_decision(evidence: dict) -> dict:
    blockers = []
    for key in REQUIRED:
        item = evidence.get(key)
        if not isinstance(item, dict) or item.get('passed') is not True:
            blockers.append(key)
            continue
        if not item.get('artifact') or not item.get('tested_at'):
            blockers.append(key + ':evidence_missing')
    live = evidence.get('real_intraday_shadow') or {}
    if live.get('passed') is True and (live.get('mode') != 'live_shadow'
                                      or live.get('market_open') is not True
                                      or live.get('fresh_quotes') is not True):
        blockers.append('real_intraday_shadow:not_live_evidence')
    return {'deployment_allowed': not blockers, 'blockers': blockers,
            'orders_authorized': False, 'profitability_validated': False}
