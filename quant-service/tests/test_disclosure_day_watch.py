"""Real-PostgreSQL coverage for the disclosure-day watch proposal source.

The prior-guidance exclusion is the module's entire finding (3.77% vs 1.60%
same-day limit-up over the measured window), so it is pinned here against a
real schema rather than a mock.
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timezone

from app.disclosure_day_watch import next_trading_session, rank_disclosure_watch, scheduled_disclosures
from app.main import db
from app.watchlist_candidate_proposals import materialize_disclosure_day_watch, materialize_watchlist_proposals


class RankDisclosureWatchTests(unittest.TestCase):
    """Pure ranking rules, no database required."""

    @staticmethod
    def _candidate(symbol, *, forecasts=0, express=0):
        return {"symbol": symbol, "period": date(2026, 6, 30), "pre_date": date(2026, 8, 26),
                "modify_date": None, "actual_date": None,
                "forecast_count": forecasts, "express_count": express, "latest_forecast_type": None}

    @staticmethod
    def _liquid(*symbols):
        return {symbol: {"eligible": True, "flags": []} for symbol in symbols}

    def test_prior_guidance_excludes_a_name_the_market_was_already_told_about(self):
        candidates = [self._candidate("000001.SZ"), self._candidate("000002.SZ", forecasts=1),
                      self._candidate("000003.SZ", express=1)]
        result = rank_disclosure_watch(
            candidates, self._liquid("000001.SZ", "000002.SZ", "000003.SZ"),
            {"000001.SZ": 5e8, "000002.SZ": 9e8, "000003.SZ": 9e8},
        )
        self.assertEqual([item["symbol"] for item in result["selected"]], ["000001.SZ"])
        self.assertEqual(result["excluded"]["prior_guidance"], 2)
        self.assertEqual(result["excluded"]["illiquid"], 0)

    def test_illiquid_names_are_excluded_and_counted_separately(self):
        candidates = [self._candidate("000001.SZ"), self._candidate("000002.SZ")]
        liquidity = {"000001.SZ": {"eligible": True, "flags": []},
                     "000002.SZ": {"eligible": False, "flags": ["median_amount_below_floor"]}}
        result = rank_disclosure_watch(candidates, liquidity, {"000001.SZ": 5e8, "000002.SZ": 1e6})
        self.assertEqual([item["symbol"] for item in result["selected"]], ["000001.SZ"])
        self.assertEqual(result["excluded"], {"illiquid": 1, "prior_guidance": 0})

    def test_ranking_is_by_traded_value_and_top_k_bounded(self):
        candidates = [self._candidate(f"00000{index}.SZ") for index in range(1, 5)]
        traded = {"000001.SZ": 1e8, "000002.SZ": 4e8, "000003.SZ": 3e8, "000004.SZ": None}
        result = rank_disclosure_watch(
            candidates, self._liquid(*[c["symbol"] for c in candidates]), traded, top_k=2,
        )
        self.assertEqual([item["symbol"] for item in result["selected"]], ["000002.SZ", "000003.SZ"])
        self.assertEqual(result["eligible_total"], 4)

    def test_a_symbol_with_no_liquidity_context_is_excluded_not_assumed_eligible(self):
        result = rank_disclosure_watch([self._candidate("000001.SZ")], {}, {})
        self.assertEqual(result["selected"], [])
        self.assertEqual(result["excluded"]["illiquid"], 1)

    def test_evidence_records_the_measured_lift_and_its_stated_limit(self):
        result = rank_disclosure_watch([self._candidate("000001.SZ")], self._liquid("000001.SZ"), {"000001.SZ": 5e8})
        evidence = result["selected"][0]["evidence"]
        self.assertFalse(evidence["prior_guidance"])
        self.assertEqual(evidence["measured_lift"]["same_day_limit_up_rate"], 0.0377)
        self.assertIn("one reporting season", evidence["measured_lift"]["note"])


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class DisclosureDayWatchIntegrationTests(unittest.TestCase):
    as_of_date = date(2099, 3, 1)
    session = date(2099, 3, 2)
    period = date(2098, 12, 31)
    symbols = ("999971.SZ", "999972.SZ", "999973.SZ")

    def _cleanup(self) -> None:
        with db.transaction() as connection:
            for table in ("earnings_forecasts", "earnings_express", "disclosure_schedule"):
                connection.execute(f"DELETE FROM quant.{table} WHERE symbol=ANY(%s)", (list(self.symbols),))
            connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=ANY(%s)", (list(self.symbols),))
            connection.execute("DELETE FROM quant.strategy_watchlist_proposals WHERE as_of_date=%s", (self.as_of_date,))
            connection.execute("DELETE FROM quant.market_trade_calendar WHERE calendar_date=%s", (self.session,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=ANY(%s)", (list(self.symbols),))

    def setUp(self) -> None:
        self._cleanup()
        self.addCleanup(self._cleanup)
        stamp = datetime(2099, 3, 1, tzinfo=timezone.utc)
        with db.transaction() as connection:
            connection.execute(
                """INSERT INTO quant.market_trade_calendar(exchange,calendar_date,is_open,provider,available_at)
                   VALUES('SSE',%s,true,'test',%s) ON CONFLICT DO NOTHING""",
                (self.session, stamp),
            )
            for symbol in self.symbols:
                connection.execute(
                    """INSERT INTO quant.instruments(symbol,exchange,list_date,is_st)
                       VALUES(%s,'SZ',%s,false) ON CONFLICT(symbol) DO UPDATE SET list_date=EXCLUDED.list_date""",
                    (symbol, date(2010, 1, 1)),
                )
                # 20 liquid sessions so the shared liquidity screen passes.
                for offset in range(20):
                    connection.execute(
                        """INSERT INTO quant.canonical_bars_daily(
                               symbol,trading_date,close,volume,amount,is_suspended,available_at,selected_provider)
                           VALUES(%s,%s,20.0,100000,%s,false,%s,'test')""",
                        (symbol, date(2099, 2, 1) + __import__("datetime").timedelta(days=offset),
                         500_000, stamp),
                    )
                connection.execute(
                    """INSERT INTO quant.disclosure_schedule(symbol,period,provider,pre_date,available_at)
                       VALUES(%s,%s,'test',%s,%s)""",
                    (symbol, self.period, self.session, stamp),
                )
            # 999972 published guidance ahead of its report; 999973 a 快报.
            connection.execute(
                """INSERT INTO quant.earnings_forecasts(symbol,period,ann_date,provider,forecast_type,available_at)
                   VALUES(%s,%s,%s,'test','预增',%s)""",
                ("999972.SZ", self.period, date(2099, 1, 15), stamp),
            )
            connection.execute(
                """INSERT INTO quant.earnings_express(symbol,period,ann_date,provider,n_income,available_at)
                   VALUES(%s,%s,%s,'test',1000,%s)""",
                ("999973.SZ", self.period, date(2099, 1, 20), stamp),
            )

    def test_next_trading_session_and_scheduled_lookup(self) -> None:
        with db.transaction() as connection:
            self.assertEqual(next_trading_session(connection, self.as_of_date), self.session)
            rows = scheduled_disclosures(connection, self.session)
        found = {row["symbol"]: row for row in rows if row["symbol"] in self.symbols}
        self.assertEqual(set(found), set(self.symbols))
        self.assertEqual(found["999972.SZ"]["forecast_count"], 1)
        self.assertEqual(found["999973.SZ"]["express_count"], 1)
        self.assertEqual(found["999971.SZ"]["forecast_count"], 0)

    def test_only_the_unguided_name_is_proposed(self) -> None:
        with db.transaction() as connection:
            result = materialize_disclosure_day_watch(connection, self.as_of_date)
        selected = [item["symbol"] for item in result["selected"] if item["symbol"] in self.symbols]
        self.assertEqual(selected, ["999971.SZ"])
        self.assertGreaterEqual(result["excluded"]["prior_guidance"], 2)

    def test_an_already_published_report_is_no_longer_a_forward_catalyst(self) -> None:
        with db.transaction() as connection:
            connection.execute(
                "UPDATE quant.disclosure_schedule SET actual_date=%s WHERE symbol=%s",
                (date(2099, 2, 20), "999971.SZ"),
            )
            result = materialize_disclosure_day_watch(connection, self.as_of_date)
        self.assertNotIn("999971.SZ", [item["symbol"] for item in result["selected"]])

    def test_proposals_carry_the_source_label_and_no_fabricated_score(self) -> None:
        with db.transaction() as connection:
            summary = materialize_watchlist_proposals(connection, self.as_of_date)
            rows = connection.execute(
                """SELECT symbol,strategy_key,raw_score,score_scale,strategy_percentile,proposal_source
                     FROM quant.strategy_watchlist_proposals WHERE as_of_date=%s AND symbol=ANY(%s)""",
                (self.as_of_date, list(self.symbols)),
            ).fetchall()
        self.assertGreaterEqual(summary["disclosure_day_watch"], 1)
        row = next(item for item in rows if item["symbol"] == "999971.SZ")
        self.assertEqual(row["proposal_source"], "disclosure_day_watch")
        self.assertEqual(row["strategy_key"], "disclosure_day_watch")
        self.assertIsNone(row["raw_score"], "an event watch must not invent a score")
        self.assertIsNone(row["strategy_percentile"])
        self.assertEqual(row["score_scale"], "unscored_event_watch")


if __name__ == "__main__":
    unittest.main()
