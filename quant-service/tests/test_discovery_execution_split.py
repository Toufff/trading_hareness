import unittest
from unittest.mock import patch

from app.short_term_lanes.rules import Settings, screen
from app.short_term_liquidity import rerank
from test_short_term_lanes import fixture


class DiscoveryExecutionTests(unittest.TestCase):
    def market(self):
        rows = []
        for n in range(6):
            bars, sessions = fixture([10,10.1,10.2,10.3,10.4,10.5,10.6,10.7,10.8,11,12.1],
                                     [5e8]*10+[9e8], symbol=f'002{n:03}.SZ')
            rows.extend(bars)
        for n in range(10,20):
            bars, _ = fixture([10]*11, symbol=f'002{n:03}.SZ')
            for bar in bars:
                bar['plate_id'] = 'other'
            rows.extend(bars)
        return rows, sessions

    def test_limit_like_close_kept_in_breakout_and_trend_observation(self):
        rows, sessions = self.market()
        with patch('app.short_term_lanes.rules.route_regime', return_value={
            'state':'restricted', 'priority_weight':.3, 'regime':'risk_off'}):
            result = screen(rows, sessions, sessions[-1], settings=Settings(minimum_universe=1))
        for key in ('expansion', 'trend'):
            lane = next(l for l in result['lanes'] if l['key']==key)
            self.assertGreater(lane['total_matches'], 0)
            self.assertTrue(lane['observation_list'])
            self.assertFalse(lane['selected'])
            for row in lane['observation_list']:
                self.assertFalse(row['buy_authorized'])
                self.assertEqual(row['execution']['status'], 'wait_next_session')
                self.assertIn('near_limit_up', row['execution']['flags'])

    def test_disabled_relay_is_not_deleted_and_rank_does_not_collapse(self):
        rows, sessions = self.market()
        with patch('app.short_term_lanes.rules.route_regime', return_value={
            'state':'disabled', 'priority_weight':0, 'regime':'risk_off'}):
            result = screen(rows, sessions, sessions[-1], settings=Settings(minimum_universe=1))
        lane = next(l for l in result['lanes'] if l['key']=='relay')
        self.assertEqual(lane['total_matches'], 6)
        self.assertFalse(lane['selected'])
        self.assertGreater(lane['observation_list'][0]['discovery_score'], 0)
        self.assertEqual(lane['observation_list'][0]['execution']['market_route'], 'disabled')

    def test_heat_is_not_saturated_liquidity_or_a_buy_permission(self):
        rows = []
        for symbol, amount, multiple in [('active',1.2e9,1),('hot',10e9,2.5)]:
            rows.append(dict(symbol=symbol, raw_score=10, state='watch', buy_authorized=False,
                metrics=dict(amount=amount,turnover=10,amount_multiple=multiple,
                             flow_series=[dict(amount=amount)]*5)))
        rerank(rows)
        self.assertEqual(rows[0]['liquidity']['score'], rows[1]['liquidity']['score'])
        self.assertGreater(rows[1]['attention']['participation_percentile'], rows[0]['attention']['participation_percentile'])
        self.assertEqual(rows[1]['attention']['ranking_effect'], 'shadow_only')
        self.assertFalse(rows[1]['buy_authorized'])


if __name__ == '__main__':
    unittest.main()
