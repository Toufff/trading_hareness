import unittest

from app.analysis import direction_source, extract_signals, normalize_symbol


class AnalysisExtractionTests(unittest.TestCase):
    def test_a_share_exchange_normalization(self):
        self.assertEqual(normalize_symbol("600519"), ("600519.SH", "SSE"))
        self.assertEqual(normalize_symbol("300750"), ("300750.SZ", "SZSE"))
        self.assertEqual(normalize_symbol("830799"), ("830799.BJ", "BSE"))

    def test_nearby_opinions_do_not_cancel_each_other(self):
        signals = {signal.symbol: signal for signal in extract_signals("看好600519，建议中线布局；回避300750短线风险")}
        self.assertEqual(signals["600519.SH"].direction, 1)
        self.assertEqual(signals["600519.SH"].horizon_days, 20)
        self.assertEqual(signals["300750.SZ"].direction, -1)
        self.assertEqual(signals["300750.SZ"].horizon_days, 5)

    def test_unqualified_mention_is_watch_not_trade_signal(self):
        signals = extract_signals("今天只记录000001的财报发布日期")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].direction, 0)

    def test_explicit_portfolio_actions_are_directional(self):
        signals = {signal.symbol: signal for signal in extract_signals("689009 九号公司 调入；603369 今世缘 调出")}
        self.assertEqual(signals["689009.SH"].direction, 1)
        self.assertEqual(signals["603369.SH"].direction, -1)
        self.assertEqual(direction_source(signals["689009.SH"].evidence_text), "explicit_action_positive")


if __name__ == "__main__":
    unittest.main()
