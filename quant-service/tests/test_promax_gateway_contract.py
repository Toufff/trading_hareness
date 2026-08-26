"""Contract tests for the ProMax GET gateway routing and realtime retries.

The allow-list and the single-attempt realtime rule were both built from one
2026-08-17 probe. ProMax rejects roughly one call in five with a transient
503/504, so that probe under-reported capability and the one-attempt rule
turned every transient rejection into a recorded permanent failure
(``rt_min`` reached 108 consecutive failures while succeeding on manual retry).
These tests pin the corrected behaviour.
"""

from __future__ import annotations

import unittest

from app.tushare_providers import (
    PROMAX_BOUNDED_ONLY_APIS,
    PROMAX_REALTIME_APIS,
    PROMAX_VERIFIED_APIS,
    REALTIME_RETRY_BACKOFF_SECONDS,
    REALTIME_RETRY_DEADLINE_SECONDS,
    SUPER_GET_VERIFIED_APIS,
    TushareProvider,
    _filter_requested_realtime_rows,
)


def _promax(**overrides) -> TushareProvider:
    base = {
        "name": "super_get", "key": "tushare_super_get", "label": "Tushare ProMax GET 网关",
        "endpoint": "https://example.invalid/tushare/pro", "credential": "k",
        "protocol": "get_x_api_key", "get_gateway_mode": "promax",
    }
    return TushareProvider(**{**base, **overrides})


class PromaxAllowListTests(unittest.TestCase):
    def test_the_probed_capability_set_is_routable(self):
        """Every API below returned code=0 with real rows on 2026-08-26."""
        for api in ("stk_limit", "adj_factor", "moneyflow_dc", "moneyflow_ths",
                    "disclosure_date", "forecast", "express", "limit_list_d",
                    "kpl_list", "ths_hot", "dc_hot", "top_list", "cyq_perf",
                    "rt_etf_k", "rt_idx_k", "rt_sw_k"):
            with self.subTest(api=api):
                self.assertIn(api, PROMAX_VERIFIED_APIS)
                self.assertTrue(_promax().supports(api))
                self.assertTrue(_promax().uses_super_get(api))

    def test_the_one_route_that_never_answered_stays_out(self):
        self.assertNotIn("rt_fut_min_daily", PROMAX_VERIFIED_APIS,
                         "answered HTTP 503 on all four probe attempts")
        self.assertFalse(_promax().supports("rt_fut_min_daily"))

    def test_an_undeclared_api_is_still_refused(self):
        self.assertFalse(_promax().supports("definitely_not_an_api"))

    def test_promax_and_legacy_allow_lists_stay_independent(self):
        legacy = _promax(get_gateway_mode="legacy")
        self.assertEqual(legacy.get_verified_apis, SUPER_GET_VERIFIED_APIS)
        self.assertEqual(_promax().get_verified_apis, PROMAX_VERIFIED_APIS)

    def test_realtime_set_includes_the_live_quote_routes(self):
        # ProMax rt_k carries a second-resolution updated_at plus level-1
        # bid/ask; the City SDK's rt_k is the delayed one, and the two must not
        # be conflated.
        self.assertEqual(
            PROMAX_REALTIME_APIS,
            {"rt_k", "rt_min", "rt_min_daily", "rt_etf_k", "rt_idx_k", "rt_sw_k", "rt_fut_min"},
        )
        for api in PROMAX_REALTIME_APIS:
            self.assertIn(api, PROMAX_VERIFIED_APIS)

    def test_bounded_only_routes_are_declared(self):
        # stk_factor_pro rejects a full-market trade_date cross-section (400)
        # but serves a per-symbol range.
        self.assertIn("stk_factor_pro", PROMAX_BOUNDED_ONLY_APIS)
        self.assertTrue(_promax().requires_bounded_request("stk_factor_pro"))
        self.assertFalse(_promax().requires_bounded_request("daily"))


class RealtimeRetryBudgetTests(unittest.TestCase):
    def test_deadline_leaves_room_for_more_than_one_attempt_inside_a_scan(self):
        self.assertGreater(REALTIME_RETRY_DEADLINE_SECONDS, REALTIME_RETRY_BACKOFF_SECONDS * 2)
        self.assertLess(REALTIME_RETRY_DEADLINE_SECONDS, 30.0,
                        "must stay well inside the 30s live scan cadence")


class BatchedRealtimeFilterTests(unittest.TestCase):
    rows = [
        {"ts_code": "000001.SZ", "vol": 1},
        {"ts_code": "600519.SH", "vol": 2},
        {"ts_code": "999999.SZ", "vol": 3},
    ]

    def test_a_batched_request_keeps_every_requested_code(self):
        kept = _filter_requested_realtime_rows("rt_k", {"ts_code": "000001.SZ,600519.SH"}, self.rows)
        self.assertEqual([row["ts_code"] for row in kept], ["000001.SZ", "600519.SH"])

    def test_extra_codes_the_gateway_volunteers_are_dropped(self):
        kept = _filter_requested_realtime_rows("rt_k", {"ts_code": "000001.SZ"}, self.rows)
        self.assertEqual([row["ts_code"] for row in kept], ["000001.SZ"])

    def test_whitespace_and_case_in_a_batch_are_tolerated(self):
        kept = _filter_requested_realtime_rows("rt_k", {"ts_code": " 000001.sz , 600519.SH "}, self.rows)
        self.assertEqual(len(kept), 2)

    def test_non_realtime_routes_are_left_untouched(self):
        kept = _filter_requested_realtime_rows("daily", {"ts_code": "000001.SZ"}, self.rows)
        self.assertEqual(len(kept), 3)


if __name__ == "__main__":
    unittest.main()
