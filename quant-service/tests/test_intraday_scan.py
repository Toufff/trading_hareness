import copy
import unittest
from app.intraday_scan.rules import evaluate, build, digest


class IntradayScanTests(unittest.TestCase):
    def setUp(self):
        self.item = dict(symbol='603989.SH',name='艾华集团',lane='reclaim',source='previous',
                         reference=33.1,support=32.8,original_reason='修复',origin_id='original')
        self.quote = dict(symbol='603989.SH',name='艾华集团',close=34,amount=5e8,
                          pct_chg=3,plate_id='x',main_net=1e7,turnover_rate=4,volume_ratio=1.5)
        self.minutes = [dict(time=t,close=p,vwap=33,volume=100) for t,p in
                        [('1127',32.9),('1128',33.2),('1129',33.3),('1130',34)]]
        self.cutoff='2026-09-14T11:30:00+08:00'

    def test_new_plan_never_backdates_entry(self):
        r=evaluate(self.item,self.quote,self.minutes,self.cutoff,2)
        self.assertEqual(r['state'],'confirmed_observation')
        self.assertEqual(r['plan']['state'],'armed')
        self.assertIsNone(r['plan']['triggered_at'])

    def test_future_minutes_do_not_change_output(self):
        a=evaluate(self.item,self.quote,self.minutes,self.cutoff,2)
        b=evaluate(self.item,self.quote,self.minutes+[dict(time='1301',close=1,vwap=30)],self.cutoff,2)
        self.assertEqual(a,b)

    def test_same_cutoff_deterministic(self):
        self.assertEqual(digest(evaluate(self.item,self.quote,self.minutes,self.cutoff,2)),
                         digest(evaluate(self.item,self.quote,self.minutes,self.cutoff,2)))

    def test_missing_quote_preserves_candidate(self):
        r=evaluate(self.item,None,[],self.cutoff,2)
        self.assertEqual(r['state'],'data_gap');self.assertEqual(r['origin_id'],'original')

    def test_falling_candidate_not_deleted(self):
        q={**self.quote,'close':31,'pct_chg':-6}
        m=[{**x,'close':31} for x in self.minutes]
        r=evaluate(self.item,q,m,self.cutoff,2)
        self.assertEqual(r['state'],'invalidated')

    def test_near_limit_not_claimed_fill(self):
        q={**self.quote,'pct_chg':10,'close':36}
        r=evaluate(self.item,q,[{**x,'close':36} for x in self.minutes],self.cutoff,2)
        self.assertEqual(r['state'],'execution_uncertain')
        self.assertFalse(r['buy_authorized'])

    def test_old_frozen_plan_can_trigger_only_after_creation(self):
        old=dict(reference=33.1,support=32.8,created_at='2026-09-14T11:27:00+08:00',
                 expires_on='2026-09-14',state='armed',triggered_at=None)
        r=evaluate(self.item,self.quote,self.minutes,self.cutoff,2,previous_plan=old)
        self.assertEqual(r['plan']['state'],'triggered_unverified_fill')
        self.assertTrue(r['plan']['triggered_at'].endswith('11:30:00+08:00'))

    def test_no_reuse_expired_plan(self):
        old=dict(reference=1,support=.5,created_at='2026-09-11T11:00:00+08:00',expires_on='2026-09-11',state='armed')
        r=evaluate(self.item,self.quote,self.minutes,self.cutoff,2,previous_plan=old)
        self.assertEqual(r['previous_plan_state'],'expired')
        self.assertEqual(r['plan']['state'],'armed')

    def test_minute_gap_is_not_confirmation(self):
        m=[{**x,'close':34} for x in self.minutes if x['time']!='1129']
        r=evaluate(self.item,self.quote,m,self.cutoff,2)
        self.assertEqual(r['state'],'wait_confirmation')

    def test_same_time_volume_unavailable_is_not_shrinkage(self):
        r=evaluate(self.item,self.quote,self.minutes,self.cutoff,2)
        self.assertEqual(r['same_time_amount_status'],'unavailable')

    def test_holdings_not_an_input(self):
        import inspect
        self.assertNotIn('holdings',inspect.signature(build).parameters)

    def test_future_history_rejected(self):
        with self.assertRaisesRegex(ValueError,'settled'):
            build(dict(cutoff=self.cutoff,rows=[],history=[],sessions=['2026-09-14'],seeds=[],minutes={},health={}))

    def test_source_quote_must_match_minute_cutoff(self):
        q={**self.quote,'close':40}
        r=evaluate(self.item,q,self.minutes,self.cutoff,2)
        self.assertEqual(r['state'],'data_gap')

    def test_five_minute_vendor_windows(self):
        from app.intraday_scan.source import session_cutoff,window_params
        from datetime import datetime
        self.assertEqual(session_cutoff(datetime.fromisoformat('2026-09-14T14:07:30+08:00')).strftime('%H%M'),'1405')
        self.assertEqual(session_cutoff(datetime.fromisoformat('2026-09-14T12:15:30+08:00')).strftime('%H%M'),'1130')
        self.assertEqual(window_params('881270','1405',size=1000)['st'],300)

    def test_all_minutes_failed_cannot_publish_empty_success(self):
        from app.intraday_scan.rules import validate_minute_health
        with self.assertRaisesRegex(ValueError,'No usable minute'):
            validate_minute_health({}, {'603989.SH':'timeout'})
        self.assertEqual(validate_minute_health({'603989.SH':self.minutes},{'other':'timeout'}),'partial')

    def test_new_plan_actual_availability_not_snapshot_time(self):
        r=evaluate(self.item,self.quote,self.minutes,self.cutoff,2,plan_available_at='2026-09-14T12:20:00+08:00')
        self.assertEqual(r['plan']['created_at'],'2026-09-14T12:20:00+08:00')

    def test_overview_keeps_previous_intraday_front_runners(self):
        from app.intraday_scan.reports import render
        r=evaluate(self.item,self.quote,self.minutes,self.cutoff,2)
        result=dict(version='test',cutoff=self.cutoff,input_hash='hash',previous=[],history_through='2026-09-11',
                    market=dict(symbols=1,up=1,down=0,median=3),
                    lanes=[dict(key='reclaim',label='修复',top=[r],items=[r],discovery_scope='inherited')])
        self.assertIn('艾华集团',render(result)['overview'])

if __name__=='__main__':unittest.main()
