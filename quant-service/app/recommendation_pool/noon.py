"""Point-in-time noon scan source for the shared recommendation contract."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from ..intraday_scan import engine
from ..intraday_scan.rules import digest as intraday_digest
from ..short_term_lanes.reviews import from_recommendation, persist_rows
from .rules import compile_decision, scan_hash


def _evidence_status(result):
    rows = {}
    for lane in result.get('lanes', []):
        for item in lane.get('items', []):
            gaps = []
            if item.get('minute_end') != '1130':
                gaps.append('minute_1130_missing')
            if len(item.get('ohlc') or []) < 40:
                gaps.append('daily_ohlc_under_40')
            if item.get('state') in {'data_gap', 'invalidated'}:
                gaps.append('state_' + item['state'])
            current = {'ready': not gaps, 'gaps': gaps, 'lane': lane['key'],
                       'state': item.get('state')}
            old = rows.get(item['symbol'])
            if old is None or (current['ready'] and not old['ready']):
                rows[item['symbol']] = current
    return rows


def load(database, run_id, day):
    with database.transaction() as connection:
        row = connection.execute(
            """SELECT run_id,cutoff,state,input,result FROM quant.intraday_strategy_scans
                 WHERE run_id=%s AND cutoff::date=%s""", (run_id, day),
        ).fetchone()
    if not row or row['state'] != 'completed':
        raise ValueError('no_completed_exact_date_noon_scan')
    cutoff = row['cutoff'].astimezone(ZoneInfo('Asia/Shanghai'))
    if (cutoff.hour, cutoff.minute) != (11, 30):
        raise ValueError('not_a_noon_scan')
    data, result = row['input'], row['result']
    if result.get('input_hash') != intraday_digest(data):
        raise ValueError('noon_scan_input_hash_mismatch')
    if result.get('implementation_hash') != engine.implementation_hash():
        raise ValueError('noon_scan_implementation_changed')
    scan = engine.formal(data)
    return scan, result, data, _evidence_status(result), cutoff


def persist(database, context, review, *, now=None):
    if context.get('source_kind') != 'noon':
        raise ValueError('noon_source_required')
    now = now or datetime.now(ZoneInfo('Asia/Shanghai'))
    if now >= datetime.fromisoformat(context['valid_until']):
        raise ValueError('noon_decision_window_expired')
    if datetime.fromisoformat(review.get('reviewed_at', '')) > now:
        raise ValueError('future_noon_review')
    bundle = compile_decision(context, review)
    shared_reviews = from_recommendation(bundle['reviewed'])
    with database.transaction() as connection:
        row = connection.execute(
            """SELECT cutoff,state,input,result FROM quant.intraday_strategy_scans
                 WHERE run_id=%s FOR UPDATE""", (context['run_id'],),
        ).fetchone()
        if not row or row['state'] != 'completed':
            raise ValueError('noon_scan_unavailable_during_review')
        cutoff = row['cutoff'].astimezone(ZoneInfo('Asia/Shanghai'))
        if ((cutoff.hour, cutoff.minute) != (11, 30)
                or cutoff.isoformat() != context['source_cutoff']
                or cutoff.date().isoformat() != context['as_of_date']):
            raise ValueError('noon_source_cutoff_changed_during_review')
        if row['result'].get('input_hash') != intraday_digest(row['input']):
            raise ValueError('noon_scan_changed_during_review')
        if row['result'].get('implementation_hash') != engine.implementation_hash():
            raise ValueError('noon_scan_implementation_changed_during_review')
        scan = engine.formal(row['input'])
        if scan_hash(scan) != context['scan_hash']:
            raise ValueError('noon_scan_changed_during_review')
        connection.execute(
            """INSERT INTO quant.recommendation_pool_decisions
                (decision_id,intraday_run_id,as_of_date,scan_hash,context,review,result)
                VALUES(%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(decision_id) DO NOTHING""",
            (bundle['decision_id'], context['run_id'], context['as_of_date'],
             context['scan_hash'], Json(context), Json(review), Json(bundle)),
        )
        persist_rows(connection, datetime.fromisoformat(context['source_cutoff']).date(), shared_reviews, now=now)
        saved = connection.execute(
            'SELECT result FROM quant.recommendation_pool_decisions WHERE decision_id=%s',
            (bundle['decision_id'],),
        ).fetchone()
        if not saved or saved['result'] != bundle:
            raise ValueError('noon_decision_readback_mismatch')
    return bundle
