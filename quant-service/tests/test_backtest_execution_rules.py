from __future__ import annotations

import unittest

from app.backtest_execution_rules import a_share_exit_lag


class BacktestExecutionRuleTests(unittest.TestCase):
    def test_t_plus_one_requires_exit_after_entry_day(self):
        self.assertEqual(a_share_exit_lag(1), 2)
        self.assertEqual(a_share_exit_lag(5), 6)


if __name__ == "__main__":
    unittest.main()
