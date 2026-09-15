import copy
import unittest
from unittest.mock import patch
from app.short_term_lanes.service import configured_settings
from app.short_term_lanes.reports import make_bundle

from app.ranking_factors import FactorSpec, apply_factors, build_contexts, load_profile
from app.short_term_lanes.rules import Settings, screen
from test_short_term_lanes import fixture


class RankingFactorTests(unittest.TestCase):
    def universe(self, n=25):
        return {f'002{i:03}.SZ': {'trade_date':'20260910','circ_mv':(i+1)*1e9,
                                 'total_mv':(i+1)*2e9} for i in range(n)}

    def picks(self):
        return [{'symbol':s,'rank_score':60.,'state':'watch','buy_authorized':False,
                 'confirmation':'unchanged','regime_route':{'priority_weight':1}}
                for s in reversed(list(self.universe()))]

    def test_disabled_and_wrong_lane_are_exact_noop(self):
        rows=self.picks(); original=copy.deepcopy(rows)
        apply_factors(rows,'accumulation',(),{})
        self.assertEqual(rows,original)
        specs=(FactorSpec('small_cap',strategies=('pullback',)),)
        apply_factors(rows,'accumulation',specs,build_contexts(specs,self.universe(),'2026-09-10'))
        self.assertEqual(rows,original)

    def test_small_cap_bonus_saturates_and_preserves_decisions(self):
        specs=(FactorSpec('small_cap'),); rows=self.picks()
        context=build_contexts(specs,self.universe(),'2026-09-10')
        apply_factors(rows,'accumulation',specs,context)
        by={r['symbol']:r for r in rows}
        self.assertLess(by['002000.SZ']['rank_score'],by['002001.SZ']['rank_score'])
        self.assertGreater(by['002024.SZ']['rank_score'],by['002001.SZ']['rank_score'])
        for r in rows:
            self.assertLessEqual(r['rank_score']-60,12)
            self.assertFalse(r['buy_authorized']); self.assertEqual(r['confirmation'],'unchanged')
            self.assertIn('原排名',r['factor_overlay']['explanation'])
        once=copy.deepcopy(rows)
        apply_factors(rows,'accumulation',specs,context)
        self.assertEqual(rows,once)  # no double bonus

    def test_missing_or_stale_cap_never_looks_small(self):
        u=self.universe();u['002000.SZ']['circ_mv']=None
        u['002001.SZ']['trade_date']='20260909'
        u['002002.SZ']['circ_mv']=float('nan')
        specs=(FactorSpec('small_cap'),); rows=self.picks()
        apply_factors(rows,'trend',specs,build_contexts(specs,u,'2026-09-10'))
        for r in rows:
            if r['symbol'] in ('002000.SZ','002001.SZ','002002.SZ'):
                self.assertEqual(r['rank_score'],60)
                self.assertEqual(r['factor_overlay']['factors'][0]['status'],'missing')

    def test_low_coverage_skips_whole_factor(self):
        specs=(FactorSpec('small_cap'),);u=self.universe()
        for r in list(u.values())[:10]: r.pop('circ_mv')
        c=build_contexts(specs,u,'2026-09-10')
        self.assertEqual(c['small_cap']['status'],'insufficient_coverage')
        rows=self.picks();apply_factors(rows,'event',specs,c)
        self.assertTrue(all(r['rank_score']==60 for r in rows))

    def test_total_cap_band_stable_to_order_but_not_wrong_units(self):
        specs=(FactorSpec('small_cap'),);u=self.universe()
        a=build_contexts(specs,u,'2026-09-10')['small_cap']['evidence']
        b=build_contexts(specs,{s:{**r,'total_mv':r['total_mv']*1e4,'circ_mv':r['circ_mv']*1e4}
                              for s,r in reversed(list(u.items()))},'2026-09-10')['small_cap']['evidence']
        self.assertNotEqual([a[s]['score'] for s in a],[b[s]['score'] for s in a])
        c=build_contexts(specs,dict(reversed(list(u.items()))),'2026-09-10')['small_cap']['evidence']
        self.assertEqual(a,c)

    def test_invalid_profiles_fail_explicitly(self):
        for payload in ({'factors':[{'key':'typo'}]}, {'factors':[{'key':'small_cap','weight':2}]},
                        {'factors':[{'key':'small_cap','strategies':['typo']}]},
                        {'factors':[{'key':'small_cap'},{'key':'small_cap'}]},
                        {'factors':[{'key':'small_cap','enabled':'false'}]}):
            with self.assertRaises(ValueError):load_profile(payload,{'accumulation','trend'})
        self.assertEqual(load_profile({'factors':[{'key':'small_cap','enabled':False}]},{'trend'}),())

    def test_screen_matches_and_other_lanes_unchanged(self):
        rows=[]
        for i in range(25):
            r,s=fixture([10]*11,symbol=f'002{i:03}.SZ')
            for x in r: x.update(circ_mv=(i+1)*1e9,total_mv=(i+1)*2e9)
            rows+=r
        # A tiny cold stock must not be rescued by the factor.
        cold,_=fixture([10]*11,amounts=[1e7]*11,symbol='002999.SZ')
        for x in cold:x.update(circ_mv=1e6,total_mv=2e6)
        rows+=cold
        before=screen(rows,s,s[-1],settings=Settings(minimum_universe=1))
        after=screen(rows,s,s[-1],settings=Settings(minimum_universe=1,
                     ranking_factors=(FactorSpec('small_cap',strategies=('accumulation',)),)))
        self.assertEqual(len(after['lanes']),9)
        self.assertEqual(before['coverage'],after['coverage'])
        self.assertEqual(before['lanes'][1:],after['lanes'][1:])
        self.assertEqual(before['lanes'][0]['total_matches'],after['lanes'][0]['total_matches'])
        self.assertNotEqual(before['lanes'][0]['selected'][0]['symbol'],after['lanes'][0]['selected'][0]['symbol'])
        self.assertNotIn('002999.SZ',[x['symbol'] for x in after['lanes'][0]['selected']])
        from app.short_term_lanes.selection import project
        after['company_reviews']=[];after.update(project(after,[]))
        bundle=make_bundle(after)
        doc=next(x for x in bundle['reports'] if x['key']=='accumulation')
        self.assertIn('原排名',doc['markdown']);self.assertIn('普通流通市值',doc['markdown'])
        self.assertEqual(len(bundle['reports']),10)
        self.assertIn('factor_overlay',after['review_plan'][0]['memberships'][0])

    def test_explicit_off_ignores_runtime_profile(self):
        with patch.dict('os.environ',{'QUANT_SHORT_TERM_FACTOR_PROFILE':'not-found.json'}):
            self.assertEqual(configured_settings(disabled=True).ranking_factors,())
            with self.assertRaises(FileNotFoundError):configured_settings()
