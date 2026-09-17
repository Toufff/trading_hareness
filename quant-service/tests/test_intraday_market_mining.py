"""Focused regression tests extracted from the legacy provider helper suite."""

from provider_test_support import *  # noqa: F403


class IntradayMarketMiningTests(unittest.TestCase):
    def test_ths_concept_top_stocks_requires_exact_concept_code_membership(self):
        flows = [{"sector_key": "885001.TI", "label": "精确概念", "net_amount": 321, "change_pct": 2.1, "trading_date": date(2026, 8, 10)}]
        members = [{"sector_key": "885001.TI", "symbol": "000001.SZ"}, {"sector_key": "885001.TI", "symbol": "000002.SZ"},
                   {"sector_key": "885999.TI", "symbol": "000003.SZ"}]
        quotes = {
            "000001.SZ": {"symbol": "000001.SZ", "main_net_inflow": 50, "turnover": 100},
            "000002.SZ": {"symbol": "000002.SZ", "main_net_inflow": 150, "turnover": 50},
            "000003.SZ": {"symbol": "000003.SZ", "main_net_inflow": 999, "turnover": 999},
        }
        items, coverage = ths_concept_top_stocks(flows, members, quotes, 10)
        self.assertEqual(items[0]["taxonomy_key"], "ths_concept_flow")
        self.assertEqual([stock["symbol"] for stock in items[0]["top_stocks"]], ["000002.SZ", "000001.SZ"])
        self.assertEqual(items[0]["mapped_members"], 2)
        self.assertEqual(coverage, {"flow_boards": 1, "boards_with_members": 1, "quoted_members": 2})

    def test_board_stock_mining_requires_complete_exact_membership_and_keeps_both_directions(self):
        exact_inflow = {
            "taxonomy_key": "eastmoney_industry", "sector_key": "gold", "label": "贵金属",
            "net_inflow": 500, "change_pct": 1.2, "mapped_members": 2, "quoted_members": 2,
            "member_quotes": [
                {"symbol": "000001.SZ", "name": "流入龙头", "main_net_inflow": 100, "volume_ratio": 2.1, "turnover_rate": 4.0, "pct_change": 3.0},
                {"symbol": "000002.SZ", "name": "量能不足", "main_net_inflow": 60, "volume_ratio": 1.1, "turnover_rate": 3.0, "pct_change": 1.0},
            ],
        }
        exact_outflow = {
            "taxonomy_key": "eastmoney_industry", "sector_key": "breed", "label": "养殖业",
            "net_inflow": -400, "change_pct": -1.1, "mapped_members": 1, "quoted_members": 1,
            "member_quotes": [{"symbol": "000003.SZ", "name": "流出风险", "main_net_inflow": -90, "volume_ratio": 2.0, "turnover_rate": 5.0, "pct_change": -2.5}],
        }
        partial = {
            "taxonomy_key": "eastmoney_concept", "sector_key": "unmapped", "label": "不得猜归属",
            "net_inflow": 999, "mapped_members": 2, "quoted_members": 1,
            "member_quotes": [{"symbol": "000004.SZ", "main_net_inflow": 999, "volume_ratio": 9, "turnover_rate": 9, "pct_change": 9}],
        }
        candidates, coverage, summary = board_stock_mining_candidates([exact_inflow, exact_outflow, partial])
        self.assertEqual([(item["direction"], item["symbol"]) for item in candidates], [("inflow", "000001.SZ"), ("outflow", "000003.SZ")])
        self.assertEqual(candidates[0]["setup_key"], "board_inflow_leader")
        self.assertEqual(candidates[1]["setup_key"], "board_outflow_risk")
        self.assertEqual(coverage["exact_complete_boards"], 2)
        self.assertEqual(coverage["partial_or_unmapped_boards_skipped"], 1)
        self.assertEqual(summary["returned"], 2)

    def test_limit_linkage_mining_requires_exact_relation_flow_and_activity(self):
        relations = [
            {"symbol": "000001.SZ", "shared_concepts": 2, "concept_labels": ["低空经济"],
             "leader_symbols": ["000002.SZ"], "leader_names": ["涨停龙头"]},
            {"symbol": "000003.SZ", "shared_concepts": 1, "concept_labels": ["低空经济"],
             "leader_symbols": ["000002.SZ"], "leader_names": ["涨停龙头"]},
        ]
        quotes = {
            "000001.SZ": {"symbol": "000001.SZ", "name": "联动候选", "main_net_inflow": 80,
                            "volume_ratio": 2.0, "turnover_rate": 3.0, "pct_change": 4.2},
            "000003.SZ": {"symbol": "000003.SZ", "name": "加速末段排除", "main_net_inflow": 200,
                            "volume_ratio": 4.0, "turnover_rate": 8.0, "pct_change": 7.2},
        }
        candidates, summary = limit_linkage_candidates(relations, quotes)
        self.assertEqual([item["symbol"] for item in candidates], ["000001.SZ"])
        self.assertEqual(candidates[0]["leader_symbols"], ["000002.SZ"])
        self.assertEqual(candidates[0]["risk_flags"], ["leader_linkage_research_only", "requires_minute_confirmation"])
        self.assertEqual(summary["anchors"], 1)

    def test_order_book_observation_uses_depth_and_nonnegative_cumulative_deltas(self):
        previous = {
            "bids": [{"price": 10.00, "size": 100}, {"price": 9.99, "size": 50}],
            "asks": [{"price": 10.01, "size": 120}, {"price": 10.02, "size": 60}],
            "cumulative_volume_lot": 1000, "cumulative_amount": 1_000_000,
            "outer_volume_lot": 600, "inner_volume_lot": 400,
        }
        current = {
            "bids": [{"price": 10.01, "size": 180}, {"price": 10.00, "size": 80}],
            "asks": [{"price": 10.02, "size": 90}, {"price": 10.03, "size": 50}],
            "cumulative_volume_lot": 1010, "cumulative_amount": 1_010_500,
            "outer_volume_lot": 620, "inner_volume_lot": 405,
        }
        observed = order_book_observation(current, previous)
        self.assertEqual(observed["status"], "observed")
        self.assertEqual(observed["delta_status"], "ready")
        self.assertGreater(observed["qi1"], 0)
        self.assertGreater(observed["ofi_best_level"], 0)
        self.assertEqual(observed["cumulative_volume_delta_lot"], 10.0)
        self.assertEqual(observed["interval_vwap"], 10.5)

    def test_longhu_order_book_row_preserves_a_limit_up_seal_with_empty_asks(self):
        snapshot = {"ts_code": "000001.SZ", "name": "涨停样本", "price": 10.99, "pre_close": 9.99,
                    "volume": 1000, "amount": 1_099_000, "outer_volume_lot": 610, "inner_volume_lot": 390,
                    "trade_time": "20260812130000",
                    "bids": [{"price": 10.99, "size": 557769.0}] + [{"price": 0.0, "size": 0.0}] * 4,
                    "asks": [{"price": 0.0, "size": 0.0}] * 5}
        row = order_book_row(snapshot)
        self.assertIsNotNone(row)
        self.assertTrue(row["one_sided_book"])
        self.assertEqual(row["book_side"], "bid_only")
        self.assertEqual(row["seal_volume_lot"], 557769.0)
        self.assertEqual(row["cumulative_amount"], 1_099_000)
        observed = order_book_observation(row)
        self.assertEqual(observed["qi1"], 1.0)
        self.assertEqual(observed["qi5"], 1.0)

    def test_order_book_observation_never_turns_counter_reset_into_negative_turnover(self):
        observed = order_book_observation(
            {"bids": [{"price": 10, "size": 10}], "asks": [{"price": 10.01, "size": 10}],
             "cumulative_volume_lot": 10, "cumulative_amount": 1000, "outer_volume_lot": 4, "inner_volume_lot": 6},
            {"bids": [{"price": 10, "size": 9}], "asks": [{"price": 10.01, "size": 11}],
             "cumulative_volume_lot": 100, "cumulative_amount": 10_000, "outer_volume_lot": 50, "inner_volume_lot": 50},
        )
        self.assertEqual(observed["cumulative_volume_delta_lot"], 0.0)
        self.assertEqual(observed["cumulative_amount_delta"], 0.0)
        self.assertIsNone(observed["interval_vwap"])

    def test_order_book_levels_preserve_empty_intermediate_positions_for_qi_weights(self):
        observed = order_book_observation({
            "bids": [{"price": 10, "size": 100}, {"price": 0, "size": 0}, {"price": 9.98, "size": 100}],
            "asks": [{"price": 10.01, "size": 100}, {"price": 10.02, "size": 100}, {"price": 10.03, "size": 100}],
        })
        # If the invalid bid2 were compacted out, bid3 would wrongly receive
        # the bid2 weight (0.6065), rather than its true bid3 weight (0.3679).
        self.assertAlmostEqual(observed["bid_depth_lot"], 136.79, places=2)

    def test_order_book_observation_does_not_fabricate_zero_vwap_when_amount_did_not_move(self):
        observed = order_book_observation(
            {"bids": [{"price": 10, "size": 10}], "asks": [{"price": 10.01, "size": 10}],
             "cumulative_volume_lot": 11, "cumulative_amount": 1000},
            {"bids": [{"price": 10, "size": 10}], "asks": [{"price": 10.01, "size": 10}],
             "cumulative_volume_lot": 10, "cumulative_amount": 1000},
        )
        self.assertEqual(observed["cumulative_volume_delta_lot"], 1.0)
        self.assertEqual(observed["cumulative_amount_delta"], 0.0)
        self.assertIsNone(observed["interval_vwap"])

    def test_order_book_aggregate_uses_windows_not_a_single_noisy_frame(self):
        at = datetime(2026, 8, 12, 5, 5, tzinfo=timezone.utc)
        rows = [
            {"observed_at": at - timedelta(seconds=offset), "raw": {"order_book_features": {"ofi_best_level": value}}}
            for offset, value in ((3, 2), (6, 3), (9, -1), (70, 9), (310, 99))
        ]
        aggregate = aggregate_order_book_observations(rows, at)
        self.assertEqual(aggregate["ofi_30s"], 4.0)
        self.assertEqual(aggregate["ofi_30s_sample_count"], 3)
        self.assertEqual(aggregate["ofi_1m"], 4.0)
        self.assertEqual(aggregate["ofi_5m"], 13.0)

    def test_order_book_aggregate_exposes_research_only_microstructure_proxies(self):
        at = datetime(2026, 8, 12, 5, 5, tzinfo=timezone.utc)
        rows = []
        for offset, mid, flow, seal in ((3, 10.00, 5, 100), (6, 10.02, 4, 80),
                                        (9, 10.01, -3, 60), (12, 10.03, 2, 40)):
            rows.append({"observed_at": at - timedelta(seconds=offset), "raw": {"order_book_features": {
                "ofi_best_level": flow, "book_mid": mid, "qi5": 0.25,
                "bid_depth_lot": 100, "ask_depth_lot": 100,
                "outer_inner_delta_lot": flow, "seal_volume_delta_lot": seal - 100,
            }}})
        aggregate = aggregate_order_book_observations(rows, at)
        self.assertIsNotNone(aggregate["kyle_lambda_proxy_5m"])
        self.assertEqual(aggregate["kyle_lambda_proxy_sample_count_5m"], 3)
        self.assertIsNotNone(aggregate["vpin_proxy_5m"])
        self.assertIsNotNone(aggregate["cord_sign_alignment_5m"])
        self.assertEqual(aggregate["seal_erosion_sample_count_5m"], 4)
