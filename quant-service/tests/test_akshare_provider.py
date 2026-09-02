from __future__ import annotations

import os
import unittest
from datetime import date
from unittest.mock import patch

import requests

import app.akshare_provider as akshare_provider_module
from app.akshare_provider import (
    _pool_events,
    _symbol_from_code,
    akshare_analyst_heat_supplements,
    akshare_default_http_timeout_seconds,
    akshare_index_fund_supplements,
)


class AkshareDefaultHttpTimeoutSecondsTests(unittest.TestCase):
    def test_defaults_to_thirty_seconds(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QUANT_AKSHARE_HTTP_TIMEOUT_SECONDS", None)
            self.assertEqual(akshare_default_http_timeout_seconds(), 30.0)

    def test_reads_a_valid_override(self) -> None:
        with patch.dict(os.environ, {"QUANT_AKSHARE_HTTP_TIMEOUT_SECONDS": "12.5"}, clear=False):
            self.assertEqual(akshare_default_http_timeout_seconds(), 12.5)

    def test_invalid_override_falls_back_to_default(self) -> None:
        with patch.dict(os.environ, {"QUANT_AKSHARE_HTTP_TIMEOUT_SECONDS": "nope"}, clear=False):
            self.assertEqual(akshare_default_http_timeout_seconds(), 30.0)


class EnsureRequestsDefaultTimeoutTests(unittest.TestCase):
    """WP6: AKShare's underlying requests must never hang forever.

    ``_ak()`` patches ``requests.Session.request`` exactly once so an
    AKShare call that omits ``timeout=`` gets a bounded default instead of
    occupying a ``public_source`` bounded-executor worker indefinitely.
    """

    def setUp(self) -> None:
        self._original_request = requests.Session.request
        self._original_patched_flag = akshare_provider_module._requests_default_timeout_patched
        akshare_provider_module._requests_default_timeout_patched = False
        requests.Session.request = self._original_request

    def tearDown(self) -> None:
        requests.Session.request = self._original_request
        akshare_provider_module._requests_default_timeout_patched = self._original_patched_flag

    def test_fills_in_a_default_timeout_when_the_caller_omits_one(self) -> None:
        captured: dict[str, object] = {}

        def fake_request(self, method, url, *args, **kwargs):
            captured.update(kwargs)
            return "response"

        requests.Session.request = fake_request
        akshare_provider_module._ensure_requests_default_timeout()

        session = requests.Session()
        result = session.request("GET", "https://example.invalid/data")
        self.assertEqual(result, "response")
        self.assertEqual(captured["timeout"], akshare_default_http_timeout_seconds())

    def test_does_not_override_an_explicit_timeout(self) -> None:
        captured: dict[str, object] = {}

        def fake_request(self, method, url, *args, **kwargs):
            captured.update(kwargs)
            return "response"

        requests.Session.request = fake_request
        akshare_provider_module._ensure_requests_default_timeout()

        session = requests.Session()
        session.request("GET", "https://example.invalid/data", timeout=5)
        self.assertEqual(captured["timeout"], 5)

    def test_patch_is_applied_only_once(self) -> None:
        akshare_provider_module._ensure_requests_default_timeout()
        patched_once = requests.Session.request
        akshare_provider_module._ensure_requests_default_timeout()
        self.assertIs(requests.Session.request, patched_once)


class SymbolFromCodeTests(unittest.TestCase):
    def test_shanghai_b_share_is_not_misrouted_to_beijing(self):
        # Every "9"-leading bare code used to be routed to the Beijing
        # Stock Exchange, which misclassified a Shanghai B-share (900xxx)
        # exactly like a genuine BSE listing (920xxx).
        self.assertEqual(_symbol_from_code("900901"), "900901.SH")
        self.assertEqual(_symbol_from_code("920819"), "920819.BJ")

    def test_mainboard_and_registration_board_codes(self):
        self.assertEqual(_symbol_from_code("600519"), "600519.SH")
        self.assertEqual(_symbol_from_code("000001"), "000001.SZ")
        self.assertEqual(_symbol_from_code("300750"), "300750.SZ")
        self.assertEqual(_symbol_from_code("688981"), "688981.SH")

    def test_explicit_prefix_form(self):
        self.assertEqual(_symbol_from_code("SH600519"), "600519.SH")

    def test_unrecognized_code_is_none(self):
        self.assertIsNone(_symbol_from_code("abc"))
        self.assertIsNone(_symbol_from_code(None))


class PoolEventsPublishedAtTests(unittest.TestCase):
    def test_published_at_is_aware_post_close_and_labelled(self):
        # A naive datetime.min.time() (00:00) made a limit pool that only
        # settles at the close look "already known" at market open; the
        # timestamp must now be aware and stamped past the close, with an
        # explicit availability_basis for the consuming repository to trust.
        def action(_ak):
            return [{"代码": "600000", "名称": "示例"}]

        with patch("app.akshare_provider._retry_call", return_value=[{"代码": "600000", "名称": "示例"}]):
            events = _pool_events(date(2026, 9, 1), [("stock_zt_pool_em", "limit_up_pool", action, "https://example.test")])

        self.assertEqual(len(events), 1)
        event = events[0]
        published_at = event["published_at"]
        self.assertTrue(published_at.endswith("+08:00") or "T15:30:00" in published_at)
        self.assertIn("15:30:00", published_at)
        self.assertEqual(event["availability_basis"], "post_close_publication")


class _CallRecordingAk:
    """Stand-in for the ``ak`` module that records every keyword call."""

    def __getattr__(self, name: str):
        def record(**kwargs: object) -> dict[str, object]:
            self.calls.append((name, kwargs))
            return {}
        return record

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []


class ClockUsesShanghaiCalendarTests(unittest.TestCase):
    def test_analyst_heat_supplements_uses_cn_today_not_container_clock(self):
        with patch("app.akshare_provider.cn_today", return_value=date(2026, 9, 1)), \
             patch("app.akshare_provider._collect_public", return_value=[]) as collect:
            akshare_analyst_heat_supplements("000001.SZ")
        specs = collect.call_args[0][0]
        ak = _CallRecordingAk()
        rank_action = next(action for name, *_rest, action, _limit in specs if name == "stock_analyst_rank_em")
        rank_action(ak)
        self.assertEqual(ak.calls[0], ("stock_analyst_rank_em", {"year": "2026"}))

    def test_index_fund_supplements_uses_cn_today_not_container_clock(self):
        with patch("app.akshare_provider.cn_today", return_value=date(2026, 9, 1)), \
             patch("app.akshare_provider._collect_public", return_value=[]) as collect:
            akshare_index_fund_supplements()
        specs = collect.call_args[0][0]
        ak = _CallRecordingAk()
        fund_action = next(action for name, *_rest, action, _limit in specs if name == "fund_portfolio_hold_em")
        fund_action(ak)
        self.assertEqual(ak.calls[0], ("fund_portfolio_hold_em", {"symbol": "000001", "date": "2026"}))


if __name__ == "__main__":
    unittest.main()
