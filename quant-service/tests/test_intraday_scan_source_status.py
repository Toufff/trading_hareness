import unittest

from app.intraday_scan_source_status import build_scan_source_status


class IntradayScanSourceStatusTests(unittest.TestCase):
    def test_partial_direct_quotes_do_not_claim_full_tencent_decision_coverage(self):
        status = build_scan_source_status(
            selected_symbols=["000001.SZ", "000002.SZ"],
            quotes={
                "000001.SZ": {"price_source": "tencent_watch_batch", "price_freshness": {"status": "fresh"}},
                "000002.SZ": {"price_source": "fuyao_ths_all_a_snapshot", "price_freshness": {"status": "fresh"}},
            },
            all_a_rows=[{"symbol": "000001.SZ"}], fresh_watch_rows=[{"ts_code": "000001.SZ"}],
            sina_watch_rows=[{"ts_code": "000002.SZ"}], licensed_watch_rows=[],
            licensed_watch_status={"status": "disabled"}, eastmoney_watch_flow_rows=[],
            eastmoney_watch_flow_status={"status": "unavailable", "scope": "explicit_watchlist_only"},
            derived_flow_status={"status": "fresh", "derived_symbols": 2},
            all_a_snapshot_status={"status": "cached", "age_seconds": 12}, surge_source={"provider_status": "completed"},
            priority_symbols=["000001.SZ"], rotation_pool_size=2, rotation_start_offset=1, next_rotation_offset=0,
            tushare_minutes={"000001.SZ": {"source": {"status": "completed"}}},
            fast_confirmations={"000001.SZ": {"status": "confirmed"}, "000002.SZ": {"status": "stale"}},
            board_cache_evidence={"status": "cached"}, quote_timestamp_slo_seconds=20.0,
        )

        tencent_watch = status["tencent_watch"]
        self.assertEqual(tencent_watch["status"], "partial")
        self.assertEqual(tencent_watch["decision_eligible_watch_quote_symbols"], 1)
        self.assertEqual(status["fuyao"]["all_a_only_watch_quote_symbols"], 1)
        self.assertEqual(tencent_watch["sina_fallback_watch_quote_symbols"], 1)
        self.assertEqual(status["tushare_rt_k_fast"]["status_counts"], {"confirmed": 1, "stale": 1})
        self.assertEqual(status["tushare_rt_min"]["rotation_start_offset"], 1)
        self.assertEqual(status["fuyao_ths_derived_watch_flow"]["derived_symbols"], 2)

    def test_no_direct_or_all_a_rows_is_explicitly_unavailable(self):
        status = build_scan_source_status(
            selected_symbols=["000001.SZ"], quotes={}, all_a_rows=[], fresh_watch_rows=[], sina_watch_rows=[],
            licensed_watch_rows=[], licensed_watch_status={"status": "disabled"},
            eastmoney_watch_flow_rows=[{"ts_code": "000001.SZ"}], all_a_snapshot_status={"status": "unavailable"},
            eastmoney_watch_flow_status={"status": "fresh", "scope": "explicit_watchlist_only"},
            derived_flow_status={"status": "unavailable"},
            surge_source={}, priority_symbols=[], rotation_pool_size=1, rotation_start_offset=0, next_rotation_offset=0,
            tushare_minutes={}, fast_confirmations={}, board_cache_evidence={}, quote_timestamp_slo_seconds=45.0,
        )
        self.assertEqual(status["tencent_watch"]["status"], "unavailable")
        self.assertEqual(status["eastmoney_watch_flow"]["status"], "completed")
        self.assertTrue(status["eastmoney_watch_flow"]["research_confirmation_only"])
        self.assertEqual(status["fuyao_ths_derived_watch_flow"]["status"], "unavailable")
        self.assertEqual(status["tencent_watch"]["missing_direct_watch_quote_symbols"], 1)


if __name__ == "__main__":
    unittest.main()
