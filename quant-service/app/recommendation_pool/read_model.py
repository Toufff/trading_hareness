"""Bounded active noon decision projection for existing watchlist surfaces."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


NOON_ACTIVE_SQL = """SELECT result,context FROM quant.recommendation_pool_decisions
    WHERE intraday_run_id IS NOT NULL AND result->>'status'='ready'
      AND (result->>'valid_until')::timestamptz>%s AND created_at<=%s
    ORDER BY as_of_date DESC,created_at DESC,decision_id DESC LIMIT 1"""


def active_noon(database, now=None):
    now = now or datetime.now(ZoneInfo('Asia/Shanghai'))
    with database.transaction() as connection:
        return connection.execute(NOON_ACTIVE_SQL, (now, now)).fetchone()


async def active_noon_async(async_database, now=None):
    now = now or datetime.now(ZoneInfo('Asia/Shanghai'))
    async with async_database.transaction() as connection:
        cursor = await connection.execute(NOON_ACTIVE_SQL, (now, now))
        return await cursor.fetchone()


def watchlist_payload(row):
    """Project the frozen full candidate context without re-running a scan."""
    from ..short_term_lanes.rules import LANES
    labels = {key: label for key, label, _ in LANES}
    context, decision = row['context'], row['result']
    lanes = {}
    for candidate in context['candidates']:
        for membership in candidate.get('memberships') or []:
            lane = lanes.setdefault(membership['lane'], {'key': membership['lane'],
                            'label': labels.get(membership['lane'], membership['lane']),
                            'observation_list': []})
            lane['observation_list'].append((membership['rank'], {
                'symbol': candidate['symbol'], 'name': candidate['name'],
                'reason': (candidate.get('evidence') or {}).get('reason'),
                'metrics': (candidate.get('evidence') or {}).get('metrics'),
                'sector_label': candidate.get('sector_label'),
                'rank_score': membership.get('rank_score'),
            }))
    for lane in lanes.values():
        lane['observation_list'] = [item for _, item in sorted(
            lane['observation_list'], key=lambda pair: (pair[0], pair[1]['symbol']))]
    reviews = [{'symbol': item['symbol'], 'business': item.get('business'),
                'risk': item.get('company_risk'), 'conclusion': item.get('comparison'),
                'selection': {'disposition': 'retain_watch' if item['decision'] == 'recommend'
                              else 'exclude' if item['decision'] == 'exclude' else 'downgrade_watch'}}
               for item in decision.get('reviewed') or []]
    return {'latest_completed': {'as_of_date': context['as_of_date'],
        'summary': {'strategy_lanes': {'as_of_date': context['as_of_date'],
                                      'status': 'completed', 'lanes': list(lanes.values()),
                                      'company_reviews': reviews},
                    'recommendation_pool': decision}}}
