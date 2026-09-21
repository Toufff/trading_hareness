from __future__ import annotations

import threading
import unittest
from unittest.mock import patch
from datetime import date, datetime, timezone

from app.longhu_vendor_source import (
    MAX_PAGE_SIZE,
    LonghuVendorConfig,
    LonghuVendorSource,
    SharedLonghuReadSource,
    longhu_security_id,
    normalize_stock_symbol,
    parse_daily_kline_payload,
    parse_industry_stock_row,
    parse_stock_minute_payload,
    parse_stock_snapshot_payload,
    safe_page_size,
)


class LonghuVendorSourceTests(unittest.TestCase):
    def test_safe_page_size_never_exceeds_vendor_hard_limit(self):
        self.assertEqual(safe_page_size(1), 1)
        self.assertEqual(safe_page_size(300), MAX_PAGE_SIZE)
        self.assertEqual(safe_page_size(2_000), MAX_PAGE_SIZE)
        with self.assertRaises(ValueError):
            safe_page_size(0)

    def test_config_requires_complete_credentials(self):
        with self.assertRaises(ValueError):
            LonghuVendorConfig.from_mapping({"token": "x", "user_id": "", "device_id": "d"})

    def test_industry_row_preserves_vendor_flow_semantics(self):
        row = [None] * 63
        row[0], row[1] = "600664", "哈药股份"
        row[5], row[6], row[7] = 9.49, 2.15, 1_250_000_000
        row[13], row[21], row[25] = 83_000_000, 1.22, 8.65
        row[37], row[38], row[53], row[61] = 25_000_000_000, 20_000_000_000, 2.4, 18.6
        parsed = parse_industry_stock_row(row, date(2026, 9, 1), "881155")
        self.assertEqual(parsed["symbol"], "600664.SH")
        self.assertEqual(parsed["main_net"], 83_000_000)
        self.assertEqual(parsed["flow_convention"], "longhuvip_zs_stocklist_main_net_field13")
        self.assertEqual(parsed["pe"], 18.6)
        self.assertEqual(parsed["pb"], 2.4)

    def test_daily_kline_is_dated_unadjusted_lots_and_thousand_cny(self):
        payload = {"x": ["20260915", "20260916", "20260917"],
                   "y": [[15.86, 16.36, 16.53, 15.86], [16.37, 16.85, 16.93, 16.21], [16.68, 16.39, 16.91, 16.35]],
                   "vol": [2180763, 2596268, 2111340], "bal": [3550286442, 4321902474, 3501552411]}
        rows = parse_daily_kline_payload(payload, "002185.SZ", "20260916", "20260917")
        self.assertEqual([row["trade_date"] for row in rows], ["20260916", "20260917"])
        self.assertEqual((rows[1]["open"], rows[1]["close"], rows[1]["high"], rows[1]["low"]), (16.68, 16.39, 16.91, 16.35))
        self.assertEqual((rows[1]["pre_close"], rows[1]["vol"], rows[1]["amount"]), (16.85, 2111340, 3501552.411))
        self.assertEqual(parse_daily_kline_payload({**payload, "y": payload["y"][:2]}, "002185.SZ", "20260901", "20260917"), [])

    def test_indexes_need_an_exchange_and_map_to_vendor_ids(self):
        self.assertEqual(longhu_security_id("000001.SH"), ("000001.SH", "SH000001"))
        self.assertEqual(longhu_security_id("399001.SZ"), ("399001.SZ", "SZ399001"))
        self.assertEqual(longhu_security_id("000001"), ("000001.SZ", "000001"))
        self.assertEqual(longhu_security_id("002185.SZ"), ("002185.SZ", "002185"))
        self.assertEqual(longhu_security_id("sh000300"), ("000300.SH", "SH000300"))
        self.assertIsNone(longhu_security_id("399001"))

    def test_symbol_normalization_is_explicit(self):
        self.assertEqual(normalize_stock_symbol("600664"), "600664.SH")
        self.assertEqual(normalize_stock_symbol("002212"), "002212.SZ")
        self.assertEqual(normalize_stock_symbol("920895"), "920895.BJ")
        self.assertIsNone(normalize_stock_symbol("399001"))

    def test_shanghai_b_share_is_not_misrouted_to_beijing(self):
        # A single leading-digit "9 -> BJ" rule used to route both a
        # Shanghai B-share (900xxx) and a genuine BSE listing (920xxx) to
        # Beijing; they must resolve to their own real exchange.
        self.assertEqual(normalize_stock_symbol("900901"), "900901.SH")
        self.assertEqual(normalize_stock_symbol("920819"), "920819.BJ")

    def test_sh_index_is_not_misrouted_to_a_nonexistent_shenzhen_stock(self):
        # Taking the trailing 6 digits and routing "0" prefixes to SZ used
        # to turn CSI 300 (sh000300, a Shanghai index) into "000300.SZ".
        self.assertIsNone(normalize_stock_symbol("sh000300"))
        self.assertIsNone(normalize_stock_symbol("sh000001"))
        self.assertIsNone(normalize_stock_symbol("sh000688"))

    def test_bare_ambiguous_code_still_resolves_to_the_real_stock(self):
        # Without an explicit "sh" index marker, 000001 is Ping An Bank.
        self.assertEqual(normalize_stock_symbol("000001"), "000001.SZ")

    def test_stock_snapshot_keeps_vendor_exchange_timestamp(self):
        parsed = parse_stock_snapshot_payload({
            "code": "600664", "name": "哈药股份", "day": "20260901", "preclose_px": 9.29,
            "real": {
                "last_px": 9.49, "open_px": 9.30, "high_px": 9.58, "low_px": 9.18,
                "time": "145901000", "px_change_rate": 2.15, "total_amount": 123456,
                "total_turnover": 125000000, "turnover_ratio": 8.65, "vol_ratio": 1.22,
            },
        }, "600664.SH")
        self.assertEqual(parsed["ts_code"], "600664.SH")
        self.assertEqual(parsed["trade_time"], "20260901145901")
        self.assertEqual(parsed["price"], 9.49)

    def test_stock_snapshot_carries_lot_depth_and_active_side_volume(self):
        weituo = {"b1": [16.39, 2519], "b2": [16.38, 3627], "b3": [0, 0], "s1": [16.4, 3944], "s2": [16.41, 1147]}
        parsed = parse_stock_snapshot_payload({
            "code": "002185", "name": "华天科技", "day": "20260917", "preclose_px": 16.85, "weituo": weituo,
            "real": {"last_px": 16.39, "time": "150003000", "total_amount": 2111340, "total_turnover": 3501552411,
                     "amount_in": 1162931, "amount_out": 948408, "avg_px": 16.585},
        }, "002185.SZ")
        self.assertEqual(parsed["bids"][:3], [{"price": 16.39, "size": 2519}, {"price": 16.38, "size": 3627}, {"price": 0.0, "size": 0.0}])
        self.assertEqual(len(parsed["asks"]), 5)
        self.assertEqual((parsed["outer_volume_lot"], parsed["inner_volume_lot"]), (948408, 1162931))

    def test_stock_snapshot_normalizes_morning_vendor_clock_before_slicing(self):
        for raw in [93003000, '93003000', '093003000', 93003000.0, '09:30:03.000', '93003']:
            with self.subTest(raw=raw):
                parsed = parse_stock_snapshot_payload({
                    'code': '600664', 'day': '20260921', 'real': {'last_px': 8.25, 'time': raw}
                }, '600664.SH')
                self.assertEqual(parsed['trade_time'], '20260921093003')
        for raw in ['256000000', 'not-time', '999', '126100000']:
            with self.subTest(raw=raw):
                parsed = parse_stock_snapshot_payload({
                    'code': '600664', 'day': '20260921', 'real': {'last_px': 8.25, 'time': raw}
                }, '600664.SH')
                self.assertIsNone(parsed['trade_time'])

    def test_stock_minutes_are_normalized_for_existing_feature_engine(self):
        rows = parse_stock_minute_payload({
            "trend": [
                ["09:30", 10.0, 10.0, 100],
                ["09:31", 10.1, 10.05, 60],
                ["13:00", 10.2, 10.08, 80],
            ],
        }, "600664.SH")
        self.assertEqual([row["volume_lot"] for row in rows], [100.0, 60.0, 80.0])
        # The trend VWAP is cumulative: 10.05 x 160 lots x 100 - 10.0 x 100 lots x 100.
        self.assertEqual((rows[1]["cumulative_amount"], rows[1]["amount"]), (160800.0, 60800.0))
        self.assertEqual(rows[2]["cumulative_segment"], 1)
        self.assertFalse(rows[-1]["is_complete"])

    def test_index_minutes_keep_the_vendor_session_date(self):
        rows = parse_stock_minute_payload({"day": "20260917", "trend": [["09:30", 3877, 3878.952, 3717122, 1]]}, "000001.SH")
        self.assertEqual((rows[0]["ts_code"], rows[0]["session_date"]), ("000001.SH", "2026-09-17"))
        self.assertEqual(parse_stock_minute_payload({"trend": [["09:30", 1, 1, 1]]}, "sh000001"), parse_stock_minute_payload({"trend": [["09:30", 1, 1, 1]]}, "000001.SH"))

    def test_missing_minute_volume_does_not_fabricate_a_zero_amount(self):
        # A None/unparseable volume field is not evidence of a genuine
        # zero-volume minute; it must not be coerced into amount=price*0*100.
        rows = parse_stock_minute_payload({
            "trend": [
                ["09:30", 10.0, 10.0, 100],
                ["09:31", 10.1, 10.05, None],
                ["09:32", 10.2, 10.08, 50],
            ],
        }, "600664.SH")
        self.assertEqual(rows[1]["volume_lot"], 0.0)
        self.assertIsNone(rows[1]["amount"])
        self.assertFalse(rows[1]["is_complete"])
        # An ordinary row with a real (even later-overridden) volume is
        # unaffected.
        self.assertIsNotNone(rows[0]["amount"])

    def test_shared_gateway_enforces_logical_cap_and_preserves_status(self):
        source = SharedLonghuReadSource("http://owner.test", "read-key")

        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {
                    "rows": [{"ts_code": "600664.SH", "price": 12.3}],
                    "source_status": {"status": "completed", "source": "longhuvip:GetStockPanKou"},
                }

        calls = []

        def get(url, *, params, timeout):
            calls.append((url, params))
            self.assertEqual(timeout, 30.0)
            return Response()

        source._session.get = get
        rows, status = source.watch_quotes(["600664.SH", "600487.SH"], max_symbols=1)
        self.assertEqual(rows, [{"ts_code": "600664.SH", "price": 12.3}])
        self.assertEqual(calls[0][1]["symbols"], "600664.SH")
        self.assertTrue(status["truncated"])
        self.assertEqual(status["transport"], "shared_gateway")

    def test_shared_gateway_forwards_full_stock_api_contract(self):
        source = SharedLonghuReadSource("http://owner.test", "read-key")

        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"target": "longhu_history", "calls": 3, "pages": []}

        calls = []

        def post(url, *, json, timeout):
            calls.append((url, json, timeout))
            return Response()

        source._session.post = post
        result = source.raw_call({
            "target": "longhu_history",
            "params": {"a": "GGList_JGCC", "c": "ZhuLiChiCang", "st": 650},
        })
        self.assertEqual(result["calls"], 3)
        self.assertEqual(calls[0][0], "http://owner.test/licensed/stock-api/call")
        self.assertEqual(calls[0][1]["params"]["st"], 650)
        self.assertGreaterEqual(calls[0][2], 180.0)

    def test_shared_gateway_replaces_stale_pool_and_retries_read_only_post_once(self):
        source = SharedLonghuReadSource("http://owner.test", "read-key")

        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"target": "longhu_history", "calls": 1, "pages": []}

        class StaleSession:
            def __init__(self):
                self.closed = False

            def post(self, *_args, **_kwargs):
                raise requests.ConnectionError("dead reverse-tunnel keepalive")

            def close(self):
                self.closed = True

        class FreshSession:
            def __init__(self):
                self.trust_env = True
                self.headers = {}
                self.calls = 0

            def post(self, *_args, **_kwargs):
                self.calls += 1
                return Response()

        import requests
        stale = StaleSession()
        fresh = FreshSession()
        source._session = stale
        with patch("app.longhu_vendor_source.requests.Session", return_value=fresh):
            result = source.raw_call({"target": "longhu_history", "params": {"st": 1}})
        self.assertEqual(result["calls"], 1)
        self.assertTrue(stale.closed)
        self.assertEqual(fresh.calls, 1)
        self.assertFalse(fresh.trust_env)
        self.assertEqual(fresh.headers["X-Quant-Read-Key"], "read-key")

    def test_plate_list_paginates_larger_logical_reads_in_300_row_batches(self):
        source = LonghuVendorSource(LonghuVendorConfig(token="t", user_id="u", device_id="d"))
        offsets = []

        def vendor_row(code):
            row = [None] * 63
            row[0], row[1], row[5], row[13] = code, code, 10.0, 1_000.0
            return row

        def request(_url, params):
            self.assertLessEqual(params["st"], MAX_PAGE_SIZE)
            offsets.append(params["Index"])
            start = params["Index"]
            size = 300 if start == 0 else 5
            return {
                "Count": 305,
                "list": [vendor_row(f"{600000 + start + index:06d}") for index in range(size)],
            }

        source._json = request
        rows = source.plate_day("881001", date(2026, 8, 31), live=False)
        self.assertEqual(offsets, [0, 300])
        self.assertEqual(len(rows), 305)

    def test_live_uses_shanghai_calendar_not_container_utc_midnight(self):
        # 2026-08-31 23:30 UTC is already 2026-09-01 07:30 in Shanghai, so a
        # container-local `date.today()` (UTC) would wrongly call the same
        # trade_date "not live" one moment and "live" the next as the UTC
        # date rolls, purely from the request's own timing.
        source = LonghuVendorSource(LonghuVendorConfig(token="t", user_id="u", device_id="d"))
        source.industry_plates = lambda: ["881001"]
        seen_live = []

        def fake_plate_day(plate_id, trade_date, *, live):
            seen_live.append(live)
            return []

        source.plate_day = fake_plate_day
        now = datetime(2026, 8, 31, 23, 30, tzinfo=timezone.utc)
        source.full_market_vendor_rows(date(2026, 9, 1), now=now)
        self.assertEqual(seen_live, [True])

    def test_live_refuses_a_non_trading_day(self):
        # 2026-09-06 is a Sunday; a non-trading day must never be treated as
        # a live session even when it equals "today" in Shanghai.
        source = LonghuVendorSource(LonghuVendorConfig(token="t", user_id="u", device_id="d"))
        source.industry_plates = lambda: ["881001"]
        seen_live = []

        def fake_plate_day(plate_id, trade_date, *, live):
            seen_live.append(live)
            return []

        source.plate_day = fake_plate_day
        sunday = date(2026, 9, 6)
        now = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
        source.full_market_vendor_rows(sunday, now=now)
        self.assertEqual(seen_live, [False])


class LonghuVendorSourceThreadLocalSessionTests(unittest.TestCase):
    """WP6: a shared ``requests.Session`` across worker threads is unsafe."""

    def test_session_is_reused_within_one_thread(self) -> None:
        source = LonghuVendorSource(LonghuVendorConfig(token="t", user_id="u", device_id="d"))
        self.assertIs(source._session, source._session)

    def test_each_thread_gets_its_own_session(self) -> None:
        source = LonghuVendorSource(LonghuVendorConfig(token="t", user_id="u", device_id="d"))
        sessions: dict[int, object] = {}
        ready = threading.Barrier(5)

        def capture() -> None:
            # Keep all workers alive concurrently.  Without a barrier a fast
            # Linux scheduler may finish one thread before the next starts and
            # legally reuse its thread id, turning a scheduling artifact into
            # a false failure of the thread-local session contract.
            ready.wait(timeout=5)
            sessions[threading.get_ident()] = source._session

        threads = [threading.Thread(target=capture) for _ in range(4)]
        for thread in threads:
            thread.start()
        ready.wait(timeout=5)
        for thread in threads:
            thread.join()

        self.assertEqual(len(sessions), 4)
        self.assertEqual(len({id(session) for session in sessions.values()}), 4)


if __name__ == "__main__":
    unittest.main()
