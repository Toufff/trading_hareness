"""One scheduled publication: collect, interpret, export and verify HTTP readback."""
import json
from datetime import datetime, timezone
from pathlib import Path

import requests

from ..automation_run_repository import run_recorded
from .pipeline import run
from .report import sections
from .schedule import due_slot
from .trading_calendar import CalendarUnavailable

TASK_KEY = 'event_research_delivery'


def verify_readback(result, api_base):
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(api_base.rstrip('/') + '/api/v1/strategy/events/latest', timeout=25)
        response.raise_for_status()
        visible = response.json()
    if visible.get('run_id') != result['run_id'] or visible.get('input_hash') != result['input_hash']:
        raise RuntimeError('event_publication_readback_mismatch')
    if visible.get('analysis', {}).get('status') != result.get('analysis', {}).get('status'):
        raise RuntimeError('event_publication_analysis_mismatch')


def deliver(db, *, output_dir, api_base, manual=False, now=None, runner=run, verifier=verify_readback):
    now = now or datetime.now(timezone.utc)
    try:
        slot = None if manual else due_slot(now)
    except CalendarUnavailable as exc:
        message = str(exc)
        def missing_calendar():
            raise CalendarUnavailable(message)
        return run_recorded(db, task_key=TASK_KEY,
                            run_key=TASK_KEY+':calendar:'+now.date().isoformat(),
                            cadence='exchange_calendar', operation=missing_calendar,
                            input_summary=dict(actual_started_at=now.isoformat(), manual=False))
    if not manual and slot is None:
        return dict(status='outside_window', reason='No scheduled news delivery due')
    key = 'manual:' + now.isoformat() if manual else slot.isoformat()
    # A session-wide advisory lock also excludes manual/scheduled overlap.
    # Held on its own transaction; model I/O uses other short-lived transactions.
    with db.transaction() as lock:
        lock.execute("SET LOCAL idle_in_transaction_session_timeout = '9min'")
        if not lock.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS acquired", (TASK_KEY,)).fetchone()['acquired']:
            return dict(status='skipped_in_flight')

        def operation():
            result = runner(db, refresh=True)
            if result['status'] not in ('analyzed', 'no_news') or result['analysis'].get('status') == 'failed':
                raise RuntimeError('event_semantic_analysis_failed:' + result['analysis'].get('failure_code', result['status']))
            if not result['source_status'] or any(s.get('status') != 'ok' or s.get('reused_capture') for s in result['source_status']):
                raise RuntimeError('event_fresh_source_incomplete')
            directory = Path(output_dir)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / (result['run_id'] + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            (directory / (result['run_id'] + '.md')).write_text('\n'.join(sections(result)), encoding='utf-8')
            verifier(result, api_base)
            return dict(status='completed', run_id=result['run_id'], reason='Fresh source, semantic analysis, report and API readback verified')

        return run_recorded(db, task_key=TASK_KEY, run_key=TASK_KEY + ':' + key,
                            cadence='exchange_days_09_12_22_closure_eve_22', operation=operation,
                            input_summary=dict(target_at=slot.isoformat() if slot and not manual else None,
                                               manual=manual, actual_started_at=now.isoformat()))
