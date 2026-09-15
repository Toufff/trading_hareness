"""Validate Luna's newly captured page before creating a broker snapshot."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .broker_fact_sync_rules import SHANGHAI, validate_exact_totals, validate_quote
from .personal_decision_contracts import BrokerPortfolioSnapshotInput


def validate_recovery(run, now, authorized):
    if not authorized:
        raise ValueError('BROKER_RECOVERY_MANUAL_AUTHORIZATION_REQUIRED')
    if run.get('status') != 'failed' or not run.get('finished_at'):
        raise ValueError('BROKER_RECOVERY_REQUIRES_FAILED_CAPTURE_RUN')
    if run['started_at'].astimezone(SHANGHAI).date() != now.astimezone(SHANGHAI).date():
        raise ValueError('BROKER_RECOVERY_DIFFERENT_DAY')


def load_trace(path, run, now=None, *, recovery=False):
    now = now or datetime.now(timezone.utc)
    trace = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if trace.get('schema_version') != 'citics-ai-ui-trace-v1' or trace.get('controller_model') != 'gpt-5.6-luna':
        raise ValueError('BROKER_UI_CONTROLLER_UNVERIFIED')
    if str(trace.get('run_id')) != str(run['run_id']) or trace.get('phase') != run['input_summary']['phase']:
        raise ValueError('BROKER_RUN_EVIDENCE_MISMATCH')
    observed = datetime.fromisoformat(trace['completed_at'])
    if observed.tzinfo is None or observed < run['started_at'] or observed > now + timedelta(seconds=5):
        raise ValueError('BROKER_OBSERVATION_TIME_INVALID')
    if recovery:
        if observed.astimezone(SHANGHAI).date() != now.astimezone(SHANGHAI).date() or observed > run['finished_at']:
            raise ValueError('BROKER_RECOVERY_OBSERVATION_INVALID')
    elif now - observed > timedelta(minutes=15):
        raise ValueError('BROKER_OBSERVATION_TIME_INVALID')
    if observed - run['started_at'] > timedelta(minutes=5):
        raise ValueError('BROKER_UI_DEADLINE_EXCEEDED')
    proof = trace.get('navigation_proof') or {}
    if proof.get('fresh_navigation_verified') is not True or proof.get('action') != 'holdings_verified':
        raise ValueError('BROKER_FRESH_NAVIGATION_MISSING')
    if proof.get('source_state') == 'detailed_holdings' and not (proof.get('forced_reentry') is True and proof.get('departure_state') in ('home', 'asset_panorama')):
        raise ValueError('BROKER_CACHED_PAGE_NOT_REENTERED')
    transitions = trace.get('transitions') or []
    if not transitions or transitions[-1].get('page_after') != 'detailed_holdings':
        raise ValueError('BROKER_TRANSITIONS_INCOMPLETE')
    evidence = []
    for step in transitions:
        if step.get('verified_by_model') is not True or not all(step.get(k) for k in ('page_before', 'page_after', 'action')):
            raise ValueError('BROKER_TRANSITION_UNVERIFIED')
        for key in ('before_screenshot', 'after_screenshot'):
            image = Path(step[key]).resolve()
            if not image.is_file() or image.read_bytes()[:8] != b'\x89PNG\r\n\x1a\n':
                raise ValueError('BROKER_SCREENSHOT_MISSING_OR_INVALID: ' + str(image))
            diagnostic = Path(str(image) + '.capture.json')
            data = json.loads(diagnostic.read_text(encoding='utf-8-sig'))
            if data.get('status') != 'success':
                raise ValueError('BROKER_CAPTURE_UNVERIFIED')
            modified = datetime.fromtimestamp(image.stat().st_mtime, timezone.utc)
            if modified < run['started_at'] - timedelta(seconds=2):
                raise ValueError('BROKER_OLD_SCREENSHOT')
            if modified > observed + timedelta(seconds=5):
                raise ValueError('BROKER_SCREENSHOT_NEWER_THAN_OBSERVATION')
            evidence.append({'path': str(image), 'sha256': hashlib.sha256(image.read_bytes()).hexdigest(), 'capture_diagnostic': str(diagnostic)})
        if Path(step['before_screenshot']).stat().st_mtime > Path(step['after_screenshot']).stat().st_mtime + 2:
            raise ValueError('BROKER_TRANSITION_TIME_REVERSED')
    final = Path(trace['final_screenshot']).resolve()
    if final != Path(transitions[-1]['after_screenshot']).resolve():
        raise ValueError('BROKER_FINAL_SCREENSHOT_MISMATCH')
    final_time = datetime.fromtimestamp(final.stat().st_mtime, timezone.utc)
    if abs((observed - final_time).total_seconds()) > 90:
        raise ValueError('BROKER_OBSERVATION_NOT_BOUND_TO_FINAL_CAPTURE')
    return trace, observed, evidence


def build_snapshot(facts, trace, observed, evidence, identities, quotes):
    if facts.get('reconciled') is not True:
        raise ValueError('BROKER_HOLDINGS_PARSE_FAILED')
    positions = facts.get('positions')
    if not isinstance(positions, list): raise ValueError('BROKER_POSITIONS_MISSING')
    validate_exact_totals(facts.get('account') or {}, positions)
    rows = []
    for position in positions:
        symbol = identities.get(position['name'])
        if not symbol: raise ValueError(f'BROKER_SECURITY_IDENTITY_AMBIGUOUS: {position["name"]}')
        validate_quote(quotes.get(symbol) or {}, position['price'], observed, trace['phase'])
        rows.append({'symbol': symbol, 'name': position['name'], 'quantity': position['quantity'],
                     'sellable_quantity': position['available_quantity'], 'average_cost': position.get('cost'),
                     'market_price': position['price'], 'market_value': position['market_value'],
                     'unrealized_pnl': position.get('profit')})
    account = facts['account']
    return BrokerPortfolioSnapshotInput.model_validate({
        'account_key': 'citics-primary', 'source': 'citics_mumu_luna',
        'source_snapshot_key': str(trace['run_id']), 'observed_at': observed,
        'verification': 'verified_exact', 'cash': account['available_cash'],
        'total_asset': account['total_assets'], 'total_market_value': account['market_value'],
        'positions': rows, 'metadata': {'contract': 'broker-fact-sync-v2', 'controller_model': 'gpt-5.6-luna',
            'fresh_navigation_verified': True, 'complete_positions_verified': True,
            'evidence': evidence, 'navigation_proof': trace['navigation_proof'],
            'quote_checks': quotes, 'broker_account_fields': account, 'cleared_rows': facts.get('cleared_rows', [])}})
