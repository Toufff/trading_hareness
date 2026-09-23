from app.recommendation_pool.read_model import watchlist_payload
from app.strategy_watchlist_projection import compact_post_close_watchlist
import asyncio


def test_noon_watchlist_projects_same_decision_and_full_candidate_context():
    row = {'context': {'as_of_date': '2026-09-23', 'candidates': [
        {'symbol': '001232.SZ', 'name': '嘉立创', 'memberships': [
            {'lane': 'trend', 'rank': 2, 'rank_score': 80},
            {'lane': 'expansion', 'rank': 1, 'rank_score': 90}],
         'sector_label': '电子', 'evidence': {'reason': '结构转强', 'metrics': {}}},
        {'symbol': '600001.SH', 'name': '对照', 'memberships': [
            {'lane': 'trend', 'rank': 1, 'rank_score': 85}],
         'sector_label': '电子', 'evidence': {'reason': '等待', 'metrics': {}}},
    ]}, 'result': {'status': 'ready', 'source_kind': 'noon', 'decision_id': 'noon-decision',
                  'recommended': [{'symbol': '001232.SZ', 'priority': 1}],
                  'reviewed': [{'symbol': '001232.SZ', 'decision': 'recommend',
                                'business': '电子制造', 'company_risk': '涨幅过快',
                                'comparison': '同轮领先'}]}}
    projected = compact_post_close_watchlist(watchlist_payload(row))
    assert projected['recommendation_pool']['decision_id'] == 'noon-decision'
    assert projected['as_of_date'] == '2026-09-23'
    jlc = next(item for item in projected['items'] if item['symbol'] == '001232.SZ')
    assert jlc['business'] == '电子制造'
    assert jlc['lane_keys'] == ['trend', 'expansion']
    assert jlc['buy_authorized'] is False


def test_watchlist_route_prefers_active_noon_decision(monkeypatch):
    from app.recommendation_pool import read_model
    from app.routers import strategy_reads

    row = {'context': {'as_of_date': '2026-09-23', 'candidates': [
        {'symbol': '001232.SZ', 'name': '嘉立创', 'memberships': [
            {'lane': 'trend', 'rank': 1, 'rank_score': 80}], 'evidence': {}}]},
           'result': {'status': 'ready', 'source_kind': 'noon', 'decision_id': 'live-noon',
                      'recommended': [{'symbol': '001232.SZ', 'priority': 1}], 'reviewed': []}}

    async def noon(_database):
        return row

    async def tracked(_database):
        return {'items': []}

    async def unexpected(_database):
        raise AssertionError('active noon must not be replaced by old postclose data')

    monkeypatch.setattr(read_model, 'active_noon_async', noon)
    monkeypatch.setattr(strategy_reads, 'async_intraday_watchlists', tracked)
    monkeypatch.setattr(strategy_reads, 'latest_post_close_strategy', unexpected)
    router = strategy_reads.build_strategy_reads_router(object(), 'v1', async_database=object())
    endpoint = next(route.endpoint for route in router.routes
                    if route.path == '/api/v1/strategy/post-close/watchlist/latest')
    payload = asyncio.run(endpoint(16))
    assert payload['recommendation_pool']['decision_id'] == 'live-noon'
    assert any(item['symbol'] == '001232.SZ' for item in payload['items'])


def test_agent_paper_does_not_read_expired_noon_pool():
    from datetime import datetime
    from app.agent_paper.context import recommendation_pool

    class Connection:
        def execute(self, sql, params):
            self.sql, self.params = sql, params
            return self

        def fetchone(self):
            return None

    connection = Connection()
    now = datetime.fromisoformat('2026-09-23T15:01:00+08:00')
    assert recommendation_pool(connection, now) is None
    assert "result->>'status'='ready'" in connection.sql
    assert "result->>'valid_until'" in connection.sql
    assert connection.params == (now, now)
