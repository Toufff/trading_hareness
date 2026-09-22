import json
import unittest

from app.intraday_actions.renderer import action_card


class ActionCardTests(unittest.TestCase):
    def event(self, action='first_buy', leg=1):
        return dict(action=action, leg=leg, symbol='000811.SZ', name='冰轮环境',
                    price=20.1, price_range=[20, 20.2], quantity=100,
                    locked_quantity=0, evidence_at='2026-09-22T02:03:00+00:00',
                    quote_at='2026-09-22T02:03:05+00:00',
                    account_at='2026-09-22T01:20:00+00:00',
                    reasons=['连续三根完整分钟收在参考位上方'], live_effect='none')

    def test_all_six_actions_are_readable_and_marked_as_simulation(self):
        for action in ('first_buy', 'add', 'reduce', 'exit', 't_buy_first', 't_sell_first'):
            result = action_card(self.event(action), simulation=True)
            text = json.dumps(result, ensure_ascii=False)
            self.assertIn('本地模拟', text)
            self.assertIn('100 股', text)
            self.assertNotIn('02:03:00', text)
            self.assertIn('10:03:00', text)

    def test_second_leg_and_locked_quantity_are_explicit(self):
        event = self.event('t_buy_first', 2)
        event['locked_quantity'] = 200
        text = json.dumps(action_card(event), ensure_ascii=False)
        self.assertIn('卖出旧仓', text)
        self.assertIn('200 股', text)
        self.assertIn('不能当日卖出', text)

    def test_unknown_action_and_raw_reason_rejected(self):
        event = self.event('bad')
        with self.assertRaises(ValueError):
            action_card(event)
        event = self.event()
        event['reasons'] = ['raw_internal_reason']
        with self.assertRaises(ValueError):
            action_card(event)

    def test_no_guaranteed_fill_or_missing_price_invented(self):
        event = self.event()
        event['price_range'] = None
        text = json.dumps(action_card(event), ensure_ascii=False)
        self.assertIn('区间未提供', text)
        self.assertIn('不代表已成交', text)

    def test_t_sell_floor_is_visible_not_confused_with_trigger(self):
        event = self.event('t_sell_first')
        event.update(price_range=None, reference=20.2, min_sell_price=20.0)
        text = json.dumps(action_card(event), ensure_ascii=False)
        self.assertIn('触发参考位 20.20', text)
        self.assertIn('最低允许卖价 20.00', text)

    def test_real_engine_output_renders_for_every_action(self):
        from test_intraday_action_engine import fixture, arm
        from app.intraday_actions.engine import evaluate
        for action in ('first_buy', 'add', 'reduce', 'exit', 't_buy_first', 't_sell_first'):
            result = evaluate(arm(fixture(action)))
            self.assertEqual(len(result['events']), 1, action)
            text = json.dumps(action_card(result['events'][0], simulation=True), ensure_ascii=False)
            self.assertIn('本地模拟', text)
            if action in ('reduce', 'exit', 't_sell_first'):
                self.assertNotIn('区间未提供', text)
                self.assertIn('触发参考位', text)


if __name__ == '__main__':
    unittest.main()
