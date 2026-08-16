from __future__ import annotations

import unittest
import json
from datetime import datetime, timezone
from decimal import Decimal

from app.analyst_prompt_lab import (
    INTRADAY_METHODOLOGY_VERSION,
    PROMPT_VARIANTS,
    _candidate_payload,
    _chronological_label_split,
    materialize_intraday_analyst_outcomes,
)
from app.analyst_action_outcomes import (
    ANQIANG_ACTION_REPLAY_METHODOLOGY_VERSION,
    materialize_anqiang_action_replay_outcomes,
)
from app.paper_execution_service import _latest_local_quote


class AnalystPromptLabContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.row = {
            "source_kind": "message", "source_id": "msg-1", "source_version": "1", "content_hash": "a" * 64,
            "received_at": datetime(2026, 8, 13, 1, tzinfo=timezone.utc), "scope": "stock",
            "subject_key": "600000.SH", "subject_label": "测试股", "action": "buy", "direction": 1,
            "horizon_days": 3, "strength": 0.8, "confidence": 0.9, "conditions": {},
            "evidence_span": "明确关注", "status": "eligible",
        }

    def test_all_variants_share_source_contract_and_never_gain_live_effect(self) -> None:
        for variant in PROMPT_VARIANTS:
            payload = _candidate_payload(self.row, variant)
            self.assertEqual(payload["source"]["received_at"], self.row["received_at"].isoformat())
            self.assertEqual(payload["contract"]["strategy_effect"], "none")
            self.assertTrue(payload["contract"]["requires_human_gold_label"])

    def test_strict_action_rejects_non_stock_context(self) -> None:
        payload = _candidate_payload({**self.row, "scope": "theme", "subject_key": "ths:foo"}, "strict_action")
        self.assertFalse(payload["candidate"])

    def test_risk_variant_does_not_accept_positive_buy(self) -> None:
        payload = _candidate_payload(self.row, "risk_first")
        self.assertFalse(payload["candidate"])

    def test_candidate_payload_normalizes_database_decimals_before_json_storage(self) -> None:
        payload = _candidate_payload({**self.row, "strength": Decimal("0.8"), "confidence": Decimal("0.9")}, "strict_action")
        self.assertEqual(payload["observation"]["strength"], 0.8)
        self.assertEqual(payload["observation"]["confidence"], 0.9)
        json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def test_prompt_labels_use_a_chronological_holdout_not_random_rows(self) -> None:
        rows = [
            {"label": "supported", "strategy_available_at": datetime(2026, 8, day, 2, tzinfo=timezone.utc)}
            for day in range(10, 15)
        ]
        split = _chronological_label_split(rows)
        self.assertEqual(split["training_days"], ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"])
        self.assertEqual(split["holdout_days"], ["2026-08-14"])
        self.assertEqual(len(split["training"]), 4)
        self.assertEqual(len(split["holdout"]), 1)


class PaperQuoteEvidenceTests(unittest.TestCase):
    def test_latest_local_quote_merges_raw_hard_flags(self) -> None:
        class Row(dict):
            pass

        class Result:
            def fetchone(self):
                return Row({"source_name": "tencent_free", "price": 10, "pct_change": 0,
                            "raw": {"is_suspended": True, "at_limit_up": True}})

        class Connection:
            def execute(self, *_args, **_kwargs):
                return Result()

        quote = _latest_local_quote(Connection(), "600000.SH", datetime.now(timezone.utc))
        self.assertTrue(quote["is_suspended"])
        self.assertTrue(quote["at_limit_up"])
        self.assertEqual(quote["source_name"], "tencent_free")


class AnalystIntradayOutcomeClockTests(unittest.TestCase):
    @staticmethod
    def _observation(available_at: datetime) -> dict[str, object]:
        return {
            "observation_id": "observation-1", "subject_key": "000001.SZ", "direction": 1,
            "strategy_available_at": available_at,
        }

    def test_lunch_crossing_horizons_do_not_query_afternoon_quotes(self) -> None:
        available_at = datetime(2026, 8, 11, 3, 25, tzinfo=timezone.utc)  # 11:25 Shanghai.
        calls: list[tuple[str, object]] = []
        inserts: list[tuple[object, ...]] = []

        class Result:
            def __init__(self, row=None, rows=None):
                self.row, self.rows = row, rows or []

            def fetchone(self):
                return self.row

            def fetchall(self):
                return self.rows

        class Connection:
            def execute(self, query, params=None):
                text = str(query)
                calls.append((text, params))
                if "FROM quant.analyst_observations" in text:
                    return Result(rows=[self_outer._observation(available_at)])
                if "source_name='tencent_free'" in text:
                    return Result(row={"observed_at": available_at, "price": "10.00", "source_name": "tencent_free"})
                if "INSERT INTO quant.analyst_intraday_outcomes" in text:
                    inserts.append(tuple(params))
                return Result()

        self_outer = self
        result = materialize_intraday_analyst_outcomes(
            Connection(), cutoff_at=datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc), limit=1,
        )

        self.assertEqual(result["outcomes"]["matured"], 0)
        self.assertEqual(result["outcomes"]["unavailable"], 4)
        # 15/30/60-minute targets cross lunch, so the outcome code must not
        # even issue an unbounded exit lookup.  The 5-minute close target is
        # allowed but has no quote at the close in this fixture.
        exit_calls = [params for query, params in calls if "source_name=%s" in query]
        self.assertTrue(exit_calls)  # 5m at the 11:30 boundary is permissible.
        self.assertTrue(all(params[-1] <= datetime(2026, 8, 11, 3, 30, tzinfo=timezone.utc) for params in exit_calls))
        self.assertTrue(all(params[1] == INTRADAY_METHODOLOGY_VERSION for params in inserts))
        self.assertTrue(all(params[3] == "unavailable" for params in inserts))
        self.assertTrue(all(params[10].obj["session_bounded"] for params in inserts))

    def test_first_quote_and_exit_are_bounded_to_their_target_windows(self) -> None:
        available_at = datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc)  # 10:00 Shanghai.
        exit_at = datetime(2026, 8, 11, 2, 5, 20, tzinfo=timezone.utc)
        inserts: list[tuple[object, ...]] = []

        class Result:
            def __init__(self, row=None, rows=None):
                self.row, self.rows = row, rows or []

            def fetchone(self):
                return self.row

            def fetchall(self):
                return self.rows

        class Connection:
            def execute(self, query, params=None):
                text = str(query)
                if "FROM quant.analyst_observations" in text:
                    return Result(rows=[self_outer._observation(available_at)])
                if "source_name='tencent_free'" in text:
                    return Result(row={"observed_at": available_at, "price": "10.00", "source_name": "tencent_free"})
                if "source_name=%s" in text:
                    query_start, query_end = params[-2:]
                    if query_start <= exit_at <= query_end:
                        return Result(row={"observed_at": exit_at, "price": "10.20", "source_name": "tencent_free"})
                    return Result(row=None)
                if "INSERT INTO quant.analyst_intraday_outcomes" in text:
                    inserts.append(tuple(params))
                return Result()

        self_outer = self
        result = materialize_intraday_analyst_outcomes(
            Connection(), cutoff_at=datetime(2026, 8, 11, 2, 7, tzinfo=timezone.utc), limit=1,
        )

        self.assertEqual(result["outcomes"]["matured"], 1)
        five_minute = next(params for params in inserts if params[2] == 5)
        self.assertEqual(five_minute[3], "matured")
        self.assertEqual(five_minute[6], exit_at)
        self.assertEqual(five_minute[8], Decimal("0.02"))
        self.assertEqual(five_minute[10].obj["clock_basis"], "strategy_available_at")
        self.assertEqual(five_minute[10].obj["reason"], "first_quote_within_target_tolerance")


class AnqiangAuthorStatedReplayTests(unittest.TestCase):
    def test_author_stated_replay_is_session_bounded_and_has_no_live_effect(self) -> None:
        stated_at = datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc)  # 10:00 Shanghai.
        exit_at = datetime(2026, 8, 11, 2, 5, 15, tzinfo=timezone.utc)
        inserts: list[tuple[object, ...]] = []

        class Result:
            def __init__(self, row=None, rows=None):
                self.row, self.rows = row, rows or []

            def fetchone(self):
                return self.row

            def fetchall(self):
                return self.rows

        class Connection:
            def execute(self, query, params=None):
                text = str(query)
                if "FROM quant.analyst_trade_actions" in text:
                    return Result(rows=[{
                        "action_id": "action-1", "symbol": "000001.SZ", "direction": 1,
                        "stated_at": stated_at, "available_at": stated_at,
                    }])
                if "source_name='tencent_free'" in text:
                    return Result(row={"observed_at": stated_at, "price": "10.00", "source_name": "tencent_free"})
                if "source_name=%s" in text:
                    query_start, query_end = params[-2:]
                    return Result(row={"observed_at": exit_at, "price": "10.10", "source_name": "tencent_free"}
                                  if query_start <= exit_at <= query_end else None)
                if "INSERT INTO quant.analyst_action_intraday_outcomes" in text:
                    inserts.append(tuple(params))
                return Result()

        result = materialize_anqiang_action_replay_outcomes(
            Connection(), cutoff_at=datetime(2026, 8, 11, 2, 7, tzinfo=timezone.utc), limit=1,
        )

        self.assertEqual(result["outcomes"]["matured"], 1)
        self.assertIn("retrospective replay only", result["data_boundary"])
        five_minute = next(params for params in inserts if params[2] == 5)
        self.assertEqual(five_minute[1], ANQIANG_ACTION_REPLAY_METHODOLOGY_VERSION)
        self.assertEqual(five_minute[3], "matured")
        self.assertEqual(five_minute[10].obj["clock_basis"], "author_stated_at")
        self.assertTrue(five_minute[10].obj["replay_only"])
        self.assertEqual(five_minute[10].obj["strategy_effect"], "none")


if __name__ == "__main__":
    unittest.main()
