import unittest

from app.intraday_actions.acceptance import REQUIRED, release_decision


class ReleaseGateTests(unittest.TestCase):
    def evidence(self):
        rows = {key: dict(passed=True, artifact='local-evidence.json',
                          tested_at='2026-09-23T10:00:00+08:00') for key in REQUIRED}
        rows['real_intraday_shadow'].update(mode='live_shadow', market_open=True,
                                           fresh_quotes=True)
        return rows

    def test_unit_pass_does_not_prove_release(self):
        self.assertFalse(release_decision({'engine_regression': {'passed': True}})['deployment_allowed'])

    def test_each_gate_is_required(self):
        for key in REQUIRED:
            rows = self.evidence()
            rows[key]['passed'] = False
            self.assertFalse(release_decision(rows)['deployment_allowed'], key)

    def test_replay_or_closed_market_cannot_impersonate_live(self):
        for key, value in [('mode', 'historical'), ('market_open', False), ('fresh_quotes', False)]:
            rows = self.evidence()
            rows['real_intraday_shadow'][key] = value
            self.assertFalse(release_decision(rows)['deployment_allowed'])

    def test_all_evidence_does_not_authorize_trades_or_claim_profit(self):
        result = release_decision(self.evidence())
        self.assertTrue(result['deployment_allowed'])
        self.assertFalse(result['orders_authorized'])
        self.assertFalse(result['profitability_validated'])
