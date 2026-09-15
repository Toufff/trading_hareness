import copy
import unittest
from app.short_term_lanes.flow_experiments import compare
from app.short_term_lanes.rules import Settings, features, mainboard
from test_short_term_lanes import fixture


class FlowSensitivityTests(unittest.TestCase):
    def test_removing_flow_does_not_mutate_baseline_or_authorize_buy(self):
        rows, sessions = fixture([10]*11, flows=[-2e7]*11)
        original = copy.deepcopy(rows)
        result = compare(rows, sessions, Settings(), features, mainboard)
        variants = {v['key']:v for v in result['variants']}
        self.assertEqual(variants['baseline']['eligible_count'], 0)
        self.assertEqual(variants['no_flow']['eligible_count'], 1)
        self.assertEqual(result['production_effect'], 'none')
        self.assertFalse(variants['no_flow']['top'][0]['buy_authorized'])
        self.assertEqual(rows, original)

    def test_missing_flow_not_silently_zero_in_paired_comparison(self):
        rows, sessions = fixture([10]*11)
        rows[2]['main_net'] = None
        result = compare(rows, sessions, Settings(), features, mainboard)
        self.assertEqual(result['inspected'], 0)
