"""Versioned recommendation evidence in the one official PostgreSQL database."""
from copy import deepcopy
from datetime import date

from psycopg.types.json import Json
from .rules import compile_decision, current_view, scan_hash
from ..short_term_lanes.reports import make_bundle
from ..short_term_lanes.reviews import from_recommendation, persist_rows
from ..short_term_lanes.selection import project

LATEST_SQL = '''SELECT result FROM quant.recommendation_pool_decisions
               WHERE run_id=%s ORDER BY created_at DESC,decision_id DESC LIMIT 1'''


def latest_target_groups(database, as_of_date):
    """Return the last persisted internal pool; broker/watchlist state is unrelated.

    Preparing a research decision must not require logging in to or reading
    Tonghuashun.  That external snapshot is required only when an already
    published decision is projected to the display-only watchlist.
    """
    with database.transaction() as connection:
        row = connection.execute(
            """SELECT result FROM quant.recommendation_pool_decisions
                 WHERE as_of_date<=%s AND result->>'status'='ready'
                 ORDER BY as_of_date DESC,created_at DESC,decision_id DESC LIMIT 1""",
            (as_of_date,),
        ).fetchone()
    result = (row or {}).get('result') or {}
    groups = result.get('target_groups') or {}
    return {key: sorted(set(groups.get(key) or [])) for key in ('推荐', '观察')}


def attach(run, row):
    if run:
        run['summary'] = {**(run.get('summary') or {}), 'recommendation_pool': current_view(
            (run.get('summary') or {}).get('strategy_lanes') or {}, row['result'] if row else None)}


def persist(database, context, review):
    if not context.get('valid_until'):
        raise ValueError('verified_next_session_expiry_required')
    result = compile_decision(context, review)
    shared_reviews = from_recommendation(result['reviewed'])
    with database.transaction() as c:
        row = c.execute('SELECT summary FROM quant.post_close_strategy_runs WHERE run_id=%s FOR UPDATE', (context['run_id'],)).fetchone()
        if not row or scan_hash(row['summary'].get('strategy_lanes') or {}) != context['scan_hash']:
            raise ValueError('scan_changed_during_review')
        c.execute('''INSERT INTO quant.recommendation_pool_decisions
                     (decision_id,run_id,as_of_date,scan_hash,context,review,result)
                     VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(decision_id) DO NOTHING''',
                  (result['decision_id'], context['run_id'], context['as_of_date'], context['scan_hash'], Json(context), Json(review), Json(result)))
        # Recommendation research and lane reports are two projections of the
        # same company evidence.  Persist it once, then refresh the existing
        # run in-place; scanner-owned fields and scan_hash remain unchanged.
        day = date.fromisoformat(context['as_of_date'])
        persist_rows(c, day, shared_reviews)
        summary = deepcopy(row['summary'])
        scan = deepcopy(summary['strategy_lanes'])
        merged = {item['symbol']: item for item in scan.get('company_reviews', [])}
        merged.update({item['symbol']: item for item in shared_reviews})
        scan['company_reviews'] = list(merged.values())
        scan.update(project(scan, scan['company_reviews']))
        for lane in scan.get('lanes', []):
            for section in ('selected', 'observation_list', 'caution_list'):
                for item in lane.get(section, []) or []:
                    if item['symbol'] in merged:
                        item['company_review'] = merged[item['symbol']]
        scan['report_bundle'] = make_bundle(scan)
        summary['strategy_lanes'] = scan
        summary['recommendation_pool'] = result
        c.execute('UPDATE quant.post_close_strategy_runs SET summary=%s WHERE run_id=%s',
                  (Json(summary), context['run_id']))
    return result
