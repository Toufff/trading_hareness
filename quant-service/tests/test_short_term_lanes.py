from datetime import date, timedelta
import unittest

from app.short_term_lanes.rules import Settings, features, screen


def fixture(prices, amounts=None, flows=None, symbol="002170.SZ", name="示例股份"):
    sessions = [str(date(2026, 8, 24)+timedelta(days=i)) for i in range(11)]
    amounts = amounts or [500_000_000]*11
    flows = flows or [20_000_000]*11
    rows = [{"symbol": symbol, "name": name, "trade_date": day, "close": p,
             "pct_chg": (p/prices[i-1]-1)*100 if i else 0, "amount": amounts[i],
             "main_net": flows[i], "turnover_rate": 5, "plate_id": "industry", "sector_label": "示例行业"}
            for i, (day, p) in enumerate(zip(sessions, prices))]
    return rows, sessions


class ShortTermLaneTests(unittest.TestCase):
    def scan(self, rows, sessions):
        return screen(rows, sessions, sessions[-1], settings=Settings(minimum_universe=1))

    def test_positive_sum_does_not_require_continuous_inflow(self):
        rows, sessions = fixture([10,10.1,10,10.1,10,10.1,10,10.1,10,10.1,10],
                                 flows=[2e7]*6+[-1e7,2e7,-1e7,2e7,2e7])
        result = self.scan(rows, sessions)
        latent = result["lanes"][0]
        self.assertEqual(latent["total_matches"], 1)
        self.assertEqual(latent["selected"][0]["metrics"]["positive_days5"], 3)
        self.assertFalse(latent["selected"][0]["buy_authorized"])

    def test_missing_day_is_not_forward_filled(self):
        rows, sessions = fixture([10]*11)
        self.assertIsNone(features(rows[:4]+rows[5:], sessions))
        self.assertEqual(self.scan(rows[:4]+rows[5:], sessions)["status"], "data_gap")

    def test_future_day_cannot_enter_ranking(self):
        rows, sessions = fixture([10]*11)
        result = self.scan(rows+[{**rows[-1], "trade_date": "2026-10-01", "close": 20}], sessions)
        self.assertEqual(result["lanes"][0]["selected"][0]["metrics"]["close"], 10)

    def test_low_activity_and_st_and_chinext_excluded(self):
        rows, sessions = fixture([10]*11, amounts=[1e7]*11)
        self.assertEqual(self.scan(rows,sessions)["lanes"][0]["total_matches"], 0)
        for symbol, name in [("300001.SZ", "创业"), ("688001.SH", "科创"), ("000001.SZ", "ST示例")]:
            r, s = fixture([10]*11,symbol=symbol,name=name)
            self.assertEqual(self.scan(r,s)["coverage"]["universe"],0)

    def test_ex_right_discontinuity_not_a_pullback(self):
        rows, sessions = fixture([10]*10+[5])
        rows[-1]["pct_chg"] = 0
        self.assertIsNone(features(rows,sessions))

    def test_sector_diversity_limits_display_not_match_count(self):
        rows=[]
        for n in range(8):
            r,s=fixture([10]*11,symbol=f"002{n:03}.SZ");rows+=r
        result=self.scan(rows,s)
        self.assertEqual(result["lanes"][0]["total_matches"],8)
        self.assertEqual(len(result["lanes"][0]["selected"]),2)

    def test_unverified_headline_not_event_benefit(self):
        rows,s=fixture([10+i*.1 for i in range(11)])
        result=screen(rows,s,s[-1],events={"002170.SZ":[{"title":"热门消息"}]}, settings=Settings(minimum_universe=1))
        self.assertEqual(result["lanes"][4]["total_matches"],0)

    def test_event_requires_structured_surprise_pricing_and_price_confirmation(self):
        target,s=fixture([10,10,10,10,10,10,10,10,10,10,10.3], amounts=[5e8]*10+[6e8])
        rows=list(target)
        for n in range(1,8):
            peer,_=fixture([10]*11,symbol=f"002{n:03}.SZ")
            rows+=peer
        event={"verified":True,"url":"https://www.cninfo.com.cn/event.pdf","benefit":"新增订单",
               "published_date":s[-1],"available_at":s[-1]+"T08:00:00+08:00","event_type":"material_contract",
               "surprise":"positive","priced_in":False,"impact_direction":"positive"}
        result=screen(rows,s,s[-1],events={"002170.SZ":[event]},settings=Settings(minimum_universe=1))
        self.assertEqual(result["lanes"][4]["total_matches"],1)
        incomplete={**event,"priced_in":"unknown"}
        result=screen(rows,s,s[-1],events={"002170.SZ":[incomplete]},settings=Settings(minimum_universe=1))
        self.assertEqual(result["lanes"][4]["total_matches"],0)

    def test_input_order_does_not_change_result(self):
        rows,s=fixture([10]*11)
        self.assertEqual(self.scan(rows,s),self.scan(list(reversed(rows)),s))

    def test_settings_serialized_for_reproduction(self):
        rows,s=fixture([10]*11)
        result=self.scan(rows,s)
        self.assertEqual(result["settings"]["minimum_amount"],300_000_000)
        self.assertEqual(len(result["lanes"]),9)
        self.assertEqual([lane["key"] for lane in result["lanes"]][-3:], ["contraction", "rotation", "reclaim"])

    def peer_market(self, prices, amounts=None):
        rows=[]
        for n in range(6):
            r,s=fixture(prices,amounts,symbol=f"002{n:03}.SZ");rows+=r
        for n in range(10,20):
            r,s=fixture([10]*11,symbol=f"002{n:03}.SZ")
            for row in r:row['plate_id']='other'
            rows+=r
        return rows,s

    def test_breakout_has_independent_list_and_not_forced_sideways(self):
        rows,s=self.peer_market([10,10.1,10.4,10.6,10.8,11,11.1,11.3,11.4,11.5,12], [5e8]*10+[8e8])
        result=self.scan(rows,s)
        self.assertGreater(result['lanes'][1]['total_matches'],0)
        self.assertGreater(result['lanes'][3]['total_matches'],0)
        self.assertEqual(result['lanes'][1]['selected'][0]['state'],'watch')

    def test_shrink_pullback_does_not_require_positive_flow(self):
        rows,s=self.peer_market([10,10.2,10.5,10.8,11,11.1,11.2,11.8,12,12.1,11.9], [5e8]*10+[3.5e8])
        for r in rows:r['main_net']=-1e6
        result=self.scan(rows,s)
        self.assertGreater(result['lanes'][2]['total_matches'],0)

    def test_relay_not_confused_with_executable_buy(self):
        rows,s=self.peer_market([10]*10+[11], [5e8]*10+[8e8])
        result=self.scan(rows,s)
        self.assertGreater(result['lanes'][5]['total_matches'],0)
        self.assertTrue(all(not r['buy_authorized'] for r in result['lanes'][5]['selected']))

    def test_dump_volume_not_rewarded_in_accumulation(self):
        rows,s=fixture([10]*10+[9.75], [5e8]*10+[10e8])
        lane=self.scan(rows,s)['lanes'][0]
        self.assertEqual(lane['selected'],[])
        # The migrated multi-window shape model may reject the broken flat
        # range outright; if it still qualifies it must stay risk-only.
        self.assertTrue(all(r['state']=='wait_recovery' for r in lane['caution_list']))

    def test_cold_intersection_retained_outside_short_term_recommendations(self):
        rows, sessions = fixture([10]*11, amounts=[1e7]*11)
        result = self.scan(rows, sessions)
        self.assertEqual(result['lanes'][0]['selected'], [])
        self.assertEqual(len(result['accumulation_observations']), 1)
        self.assertTrue(result['accumulation_observations'][0]['matched_intersection'])
        self.assertFalse(result['accumulation_observations'][0]['activity_eligible'])

    def test_sector_overview_describes_whole_sectors_as_a_label_not_a_score(self):
        rows, index = [], 0
        for plate, label, prices, count in (("industry", "示例行业", [10+i*0.2 for i in range(11)], 6),
                                            ("other", "弱势行业", [12-i*0.15 for i in range(11)], 5),
                                            ("flat", "横盘行业", [10]*11, 8),
                                            ("tiny", "小样本行业", [10]*11, 3)):
            for _ in range(count):
                r, s = fixture(prices, symbol=f"002{index:03}.SZ")
                index += 1
                for row in r:
                    row["plate_id"], row["sector_label"] = plate, label
                rows += r
        result = self.scan(rows, s)
        overview = result["sector_overview"]
        # Fewer than five complete members is not an industry median.
        self.assertEqual(set(overview), {"industry", "other", "flat"})
        self.assertEqual(overview["industry"]["relative_strength"], "strong")
        self.assertEqual(overview["other"]["relative_strength"], "weak")
        self.assertEqual(overview["flat"]["relative_strength"], "neutral")
        self.assertEqual(overview["other"]["label"], "弱势行业")
        self.assertEqual(overview["other"]["members"], 5)
        self.assertEqual(set(overview["industry"]), {
            "sector_key", "label", "members", "up_fraction", "return10_median", "change_median", "limit_up",
            "latest_breadth", "recent_breadth", "breadth_acceleration", "median_change_3d", "flow_3d",
            "stable_leaders", "relative_return10", "relative_strength"})
        self.assertAlmostEqual(overview["industry"]["relative_return10"],
                               overview["industry"]["return10_median"] - result["market"]["median_return10"])
        from copy import deepcopy
        from app.recommendation_pool.rules import scan_hash
        without = deepcopy(result)
        without["sector_overview"] = {}
        self.assertEqual(scan_hash(result), scan_hash(without))

    def test_crowded_spike_separate_not_deleted(self):
        rows,s=self.peer_market([10,10.1,10.4,10.6,10.8,11,11.1,11.3,11.4,11.5,12], [5e8]*10+[3e9])
        lane=self.scan(rows,s)['lanes'][1]
        self.assertGreater(lane['total_matches'],0)
        self.assertEqual(lane['selected'],[])
        self.assertEqual(lane['caution_list'][0]['state'],'crowded')


if __name__ == "__main__":
    unittest.main()
