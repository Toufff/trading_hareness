import unittest

from app.factor_lab import factor_at, max_drawdown, pearson, rank


class FactorLabTests(unittest.TestCase):
    def setUp(self):
        self.bars = [
            {"close": 10 + index * 0.2, "high": 10.3 + index * 0.2, "low": 9.8 + index * 0.2, "volume": 100 + index}
            for index in range(25)
        ]

    def test_rank_and_rank_ic_are_deterministic(self):
        self.assertEqual(rank([3.0, 1.0, 2.0]), [3.0, 1.0, 2.0])
        self.assertAlmostEqual(pearson(rank([1.0, 2.0, 3.0]), rank([4.0, 5.0, 6.0])) or 0, 1.0)
        self.assertIsNone(pearson([1.0], [1.0]))

    def test_native_factor_windows_and_drawdown(self):
        self.assertIsNone(factor_at(self.bars, 4, "momentum_5d"))
        self.assertGreater(factor_at(self.bars, 20, "momentum_5d") or 0, 0)
        self.assertGreater(factor_at(self.bars, 20, "sma_gap_20d") or 0, 0)
        self.assertIsNotNone(factor_at(self.bars, 20, "volatility_20d"))
        self.assertAlmostEqual(max_drawdown([1.0, 1.2, 0.9, 1.1]), -0.25)


if __name__ == "__main__":
    unittest.main()
