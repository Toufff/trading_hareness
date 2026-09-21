import json
import unittest
from unittest.mock import patch

import httpx

from app.agent_paper.jev import JevPaperModel, build_request, decode_choice
from app.agent_paper.model import ModelFailure
from app.agent_paper.rules import fees_for, normalize_order


def context():
    return {
        'now': '2026-09-21 10:00:00',
        'account': {'cash': 10000, 'positions': [
            {'symbol': '000811.SZ', 'quantity': 1000, 'sellable': 500}], 'open_orders': []},
        'detail_symbols': {'000811.SZ': {'quote': {
            'name': '冰轮环境', 'price': 10, 'quote_time': '10:00:00',
            'asks': [[10.01, 100]], 'bids': [[9.99, 100]]}}},
    }


class JevPaperTests(unittest.TestCase):
    def test_closed_or_stale_context_offers_no_trade(self):
        for field, value in [('now', '2026-09-21 20:00:00'), ('quote_time', '09:50:00')]:
            c = context()
            if field == 'now':
                c[field] = value
            else:
                c['detail_symbols']['000811.SZ']['quote'][field] = value
            _, options = build_request(c, 'jev-1.13.0')
            self.assertEqual(list(options), ['wait'])

    def test_t_plus_one_and_no_depth_remove_impossible_choices(self):
        c = context()
        c['account']['positions'][0]['sellable'] = 0
        c['detail_symbols']['000811.SZ']['quote']['asks'] = []
        _, options = build_request(c, 'jev-1.13.0')
        self.assertEqual(list(options), ['wait'])

    def test_candidates_are_board_lots_affordable_and_sellable(self):
        c = context()
        _, options = build_request(c, 'jev-1.13.0')
        self.assertGreater(len(options), 2)
        for order in options.values():
            if order is None:
                continue
            self.assertIsNotNone(normalize_order(order, positions={'000811.SZ': {'quantity': 1000}},
                                                open_order_ids=set()).order)
            if order['action'] == 'buy':
                total = order['quantity'] * order['limit_price'] + fees_for('buy', order['quantity'], order['limit_price'])
                self.assertLessEqual(total, 10000)
            else:
                self.assertLessEqual(order['quantity'], 500)

    def test_overweight_alone_is_not_an_exit_rule_and_full_cash_option_exists(self):
        c = context()
        c['account']['positions'][0]['weight_pct'] = 95
        request, options = build_request(c, 'jev-1.13.0')
        self.assertIn('wait', options)
        self.assertIn('concentration alone', request['questions']['action']['instructions'])
        self.assertGreater(max(o['quantity'] for o in options.values() if o and o['action'] == 'buy'), 500)

    def test_unknown_choice_invalid_probability_and_missing_answer_fail(self):
        _, options = build_request(context(), 'jev-1.13.0')
        answer = {'type': 'choice', 'choice': 'wait', 'confidence': .9,
                  'probabilities': {key: float(key == 'wait') for key in options}}
        payload = {'model': 'jev-1.13.0', 'answers': {'action': answer}, 'usage': {}}
        self.assertEqual(decode_choice(payload, options)['orders'], [])
        for bad in [None, {**answer, 'choice': 'invented'}, {**answer, 'confidence': float('nan')},
                    {**answer, 'probabilities': {'wait': 1}}]:
            with self.assertRaises(ModelFailure):
                decode_choice({**payload, 'answers': {'action': bad}}, options)

    def test_typed_selection_cannot_change_order_size(self):
        _, options = build_request(context(), 'jev-1.13.0')
        key = next(k for k, value in options.items() if value)
        result = decode_choice({'answers': {'action': {'type': 'choice', 'choice': key, 'confidence': 1,
                               'quantity': 99999999,
                               'probabilities': {k: float(k == key) for k in options}}}}, options)
        self.assertEqual(result['orders'][0]['quantity'], options[key]['quantity'])
        self.assertIn('程序解释', result['analysis'])

    def test_transport_uses_typed_endpoint_and_records_exact_request(self):
        seen = []
        def handle(request):
            seen.append(request)
            body = json.loads(request.content)
            options = body['questions']['action']['criteria']
            return httpx.Response(200, json={'model': 'jev-1.13.0', 'usage': {'input_tokens': 50},
                'answers': {'action': {'type': 'choice', 'choice': 'wait', 'confidence': 1,
                'probabilities': {k: float(k == 'wait') for k in options}}}})
        with patch.dict('os.environ', {'TYPESAFE_API_KEY': 'test-only', 'TYPESAFE_HTTP_PROXY': ''}):
            model = JevPaperModel(transport=httpx.MockTransport(handle))
            result = model.decide(json.dumps(context()))
        self.assertEqual(str(seen[0].url), 'https://api.typesafe.ai/v1/systemone')
        self.assertNotIn('test-only', json.dumps(result.transcript))
        self.assertIn('request', result.transcript[0])
        self.assertEqual(result.output['orders'], [])

    def test_auth_failure_is_not_retried_or_leaked(self):
        calls = []
        def fail(request):
            calls.append(request)
            return httpx.Response(401, text='test-secret-do-not-leak')
        with patch.dict('os.environ', {'TYPESAFE_API_KEY': 'test-only', 'TYPESAFE_HTTP_PROXY': ''}):
            with self.assertRaises(ModelFailure) as raised:
                JevPaperModel(transport=httpx.MockTransport(fail)).decide(json.dumps(context()))
        self.assertEqual(len(calls), 1)
        self.assertEqual(raised.exception.detail, 'HTTP 401')

    def test_invalid_typed_answer_keeps_raw_evidence(self):
        bad = {'answers': {'action': {'type': 'choice', 'choice': 'invented'}}}
        with patch.dict('os.environ', {'TYPESAFE_API_KEY': 'test-only', 'TYPESAFE_HTTP_PROXY': ''}):
            with self.assertRaises(ModelFailure) as raised:
                JevPaperModel(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=bad))).decide(json.dumps(context()))
        self.assertEqual(raised.exception.code, 'jev_invalid_answer')
        self.assertEqual(raised.exception.transcript[-1]['response'], bad)
