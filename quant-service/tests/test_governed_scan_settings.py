import unittest
from unittest.mock import patch
from app.short_term_lanes.service import governed_selection
from app.short_term_lanes.governance_checks import findings


class GovernedScanSettingsTests(unittest.TestCase):
    def test_stale_optional_config_is_isolated_not_a_whole_scan_failure(self):
        with patch.dict('os.environ', {'QUANT_SHORT_TERM_FACTOR_PROFILE':''}), patch(
            'app.strategy_governance.repository.resolve_active_config',return_value={'status':'stale_code','generation':3}):
            settings,status=governed_selection(object())
        self.assertEqual(settings.ranking_factors,())
        self.assertEqual(status['status'],'stale_code')
        issues=findings({'status':'completed','version':'test','governance_config':status})
        self.assertEqual(len(issues),1)

    def test_unavailable_governance_does_not_stop_independent_scan(self):
        with patch.dict('os.environ', {'QUANT_SHORT_TERM_FACTOR_PROFILE':''}), patch(
            'app.strategy_governance.repository.resolve_active_config',side_effect=RuntimeError('database test')):
            settings,status=governed_selection(object())
        self.assertEqual(settings.ranking_factors,())
        self.assertEqual(status['status'],'unavailable')
