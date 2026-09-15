import unittest
from unittest.mock import patch

from app.short_term_liquidity import evidence, rerank


class LiquidityTests(unittest.TestCase):
    def candidate(self, symbol, amount, raw=10, state='watch'):
        return dict(symbol=symbol, raw_score=raw, state=state,
                    metrics=dict(amount=amount, turnover=4,
                                 flow_series=[dict(amount=amount)]*5),
                    regime_route=dict(priority_weight=1), buy_authorized=False)

    def test_equal_setup_prefers_real_participation_in_every_lane(self):
        for lane in ('accumulation','expansion','pullback','trend','event','relay','contraction','rotation','reclaim'):
            rows=[self.candidate('cold',3e8),self.candidate('active',1e9)]
            rerank(rows)
            self.assertGreater(rows[1]['rank_score'],rows[0]['rank_score'],lane)
            self.assertFalse(rows[1]['buy_authorized'])

    def test_spike_is_not_sustained_liquidity_and_missing_is_not_neutral(self):
        spike=evidence(1e9,4,[2e7]*4+[1e9])
        steady=evidence(5e8,4,[5e8]*5)
        self.assertLess(spike['score'],steady['score'])
        self.assertEqual(evidence(None,4,[5e8]*5)['score'],0)
        self.assertFalse(evidence(5e8,4,[])['history_complete'])

    def test_shrink_pullback_keeps_trading_base_and_large_caps_saturate(self):
        self.assertGreater(evidence(3e8,4,[1e9]*4+[3e8])['score'],evidence(3e8,4,[3e8]*5)['score'])
        self.assertEqual(evidence(1e9,4,[1e9]*5)['score'],evidence(1e10,4,[1e10]*5)['score'])
        self.assertLess(evidence(1e9,.2,[1e9]*5)['score'],evidence(1e9,4,[1e9]*5)['score'])

    def test_risk_state_is_not_promoted_and_order_is_deterministic(self):
        rows=[self.candidate('a',5e8),self.candidate('b',1e9,state='crowded')]
        rerank(rows)
        self.assertEqual(rows[1]['state'],'crowded')
        first={r['symbol']:r['rank_score'] for r in rows}
        rerank(list(reversed(rows)))
        self.assertEqual(first,{r['symbol']:r['rank_score'] for r in rows})

    def test_actual_screen_wires_liquidity_into_all_nine_lanes(self):
        from app.short_term_lanes.rules import screen, Settings
        from test_short_term_lanes import fixture
        seen=set()
        paths=([10]*10+[10.3], [10]*10+[11],
               [10,10.2,10.5,10.8,11,11.1,11.2,11.8,12,12.1,11.9],
               [10,10.1,10.4,10.6,10.8,11,11.1,11.3,11.4,11.5,12])
        for prices in paths:
            rows=[]
            for n in range(6):
                r,s=fixture(prices,[5e8]*10+[7e8 if prices[-1]>=prices[-2] else 3.5e8],symbol=f'002{n:03}.SZ')
                rows+=r
            for n in range(10,20):
                r,_=fixture([10]*11,symbol=f'002{n:03}.SZ')
                for item in r:item['plate_id']='other'
                rows+=r
            event=dict(verified=True,url='https://www.cninfo.com.cn/example.pdf',benefit='fixture',
                       published_date=s[-1],available_at=s[-1]+'T08:00:00+08:00',event_type='contract',
                       surprise='positive',priced_in=False)
            # Mock only advanced pattern detection; the common selection,
            # rendering inputs, activity filter and all basic matches are real.
            with patch('app.short_term_lanes.rules.evaluate_advanced',return_value=(True,10,'fixture',{})):
                result=screen(rows,s,s[-1],events={'002000.SZ':[event]},
                              history_health={'ready':6},settings=Settings(minimum_universe=1))
            for lane in result['lanes']:
                for row in lane['selected']+lane['caution_list']:
                    self.assertIn('liquidity',row)
                    self.assertEqual(row['ranking_components']['liquidity_weight'],.4)
                    self.assertFalse(row['buy_authorized'])
                    seen.add(lane['key'])
        self.assertEqual(len(seen),9)
