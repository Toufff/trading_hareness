"""Coverage for the prior-session limit-up universe source.

The measured lift is large and the tradability caveat is the whole point, so
both are pinned here: nothing may present this as an entry signal.
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timezone

from app.limit_up_continuation import MEASURED, build_proposals, prior_session_limit_ups
from app.main import db
from app.watchlist_candidate_proposals import materialize_limit_up_continuation


class BuildProposalTests(unittest.TestCase):
    @staticmethod
    def _candidate(symbol, *, one_word=False, limit_ups=1):
        return {"symbol": symbol, "trading_date": date(2026, 8, 25), "close": 11.0,
                "limit_up": 11.0, "open": 11.0 if one_word else 10.2,
                "high": 11.0, "low": 11.0 if one_word else 10.1,
                "one_word": one_word, "limit_ups_20d": limit_ups}

    @staticmethod
    def _liquid(*symbols):
        return {symbol: {"eligible": True, "flags": []} for symbol in symbols}

    def test_illiquid_names_are_excluded_and_counted(self):
        liquidity = {"A.SZ": {"eligible": True, "flags": []},
                     "B.SZ": {"eligible": False, "flags": ["median_amount_below_floor"]}}
        result = build_proposals([self._candidate("A.SZ"), self._candidate("B.SZ")],
                                 liquidity, {"A.SZ": 5e8, "B.SZ": 1e6})
        self.assertEqual([item["symbol"] for item in result["selected"]], ["A.SZ"])
        self.assertEqual(result["excluded"]["illiquid"], 1)

    def test_ranking_is_by_traded_value(self):
        candidates = [self._candidate("A.SZ"), self._candidate("B.SZ"), self._candidate("C.SZ")]
        result = build_proposals(candidates, self._liquid("A.SZ", "B.SZ", "C.SZ"),
                                 {"A.SZ": 1e8, "B.SZ": 9e8, "C.SZ": 5e8})
        self.assertEqual([item["symbol"] for item in result["selected"]], ["B.SZ", "C.SZ", "A.SZ"])

    def test_one_word_boards_are_flagged_not_promoted(self):
        # They have the highest continuation rate and the worst achievable
        # return, so ranking must not favour them.
        result = build_proposals(
            [self._candidate("A.SZ", one_word=True), self._candidate("B.SZ")],
            self._liquid("A.SZ", "B.SZ"), {"A.SZ": 1e8, "B.SZ": 9e8},
        )
        self.assertEqual([item["symbol"] for item in result["selected"]], ["B.SZ", "A.SZ"])
        self.assertTrue(result["selected"][1]["one_word"])
        self.assertEqual(result["one_word_count"], 1)

    def test_every_proposal_carries_the_tradability_warning(self):
        result = build_proposals([self._candidate("A.SZ")], self._liquid("A.SZ"), {"A.SZ": 5e8})
        evidence = result["selected"][0]["evidence"]
        self.assertIn("不是入场信号", evidence["tradability_warning"])
        self.assertEqual(evidence["measured"]["limit_up_open_to_close_pct"], 0.006)
        self.assertEqual(evidence["measured"]["one_word_open_locked_rate"], 0.5542)

    def test_the_measured_block_records_both_halves_of_the_finding(self):
        self.assertGreater(MEASURED["limit_up_next_day_limit_up_rate"],
                           MEASURED["market_next_day_limit_up_rate"] * 10)
        self.assertLess(MEASURED["one_word_open_to_close_pct"], 0.0,
                        "the least buyable subset must be recorded as loss-making at the open")


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class LimitUpContinuationIntegrationTests(unittest.TestCase):
    as_of_date = date(2099, 7, 2)
    prior = date(2099, 7, 1)
    symbols = ("999961.SZ", "999962.SZ", "999963.SZ")

    def _cleanup(self) -> None:
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=ANY(%s)", (list(self.symbols),))
            connection.execute("DELETE FROM quant.strategy_watchlist_proposals WHERE as_of_date=%s", (self.as_of_date,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=ANY(%s)", (list(self.symbols),))

    def setUp(self) -> None:
        self._cleanup()
        self.addCleanup(self._cleanup)
        stamp = datetime(2099, 7, 2, tzinfo=timezone.utc)
        import datetime as _dt
        with db.transaction() as connection:
            for symbol in self.symbols:
                connection.execute(
                    """INSERT INTO quant.instruments(symbol,exchange,list_date,is_st)
                       VALUES(%s,'SZ',%s,false)""", (symbol, date(2010, 1, 1)))
                for offset in range(20):
                    connection.execute(
                        """INSERT INTO quant.canonical_bars_daily(
                               symbol,trading_date,open,high,low,close,volume,amount,
                               is_suspended,available_at,selected_provider)
                           VALUES(%s,%s,10.0,10.0,10.0,10.0,100000,500000,false,%s,'test')""",
                        (symbol, date(2099, 6, 1) + _dt.timedelta(days=offset), stamp))
            # Prior session: one locked normally, one one-word, one not locked.
            for symbol, open_, low, close, limit_up in (
                ("999961.SZ", 10.2, 10.1, 11.0, 11.0),
                ("999962.SZ", 11.0, 11.0, 11.0, 11.0),
                ("999963.SZ", 10.0, 9.9, 10.5, 11.0),
            ):
                connection.execute(
                    """INSERT INTO quant.canonical_bars_daily(
                           symbol,trading_date,open,high,low,close,limit_up,volume,amount,
                           is_suspended,available_at,selected_provider)
                       VALUES(%s,%s,%s,11.0,%s,%s,%s,100000,500000,false,%s,'test')""",
                    (symbol, self.prior, open_, low, close, limit_up, stamp))

    def test_only_locked_closes_are_selected(self) -> None:
        with db.transaction() as connection:
            rows = prior_session_limit_ups(connection, self.as_of_date)
        found = {row["symbol"]: row for row in rows if row["symbol"] in self.symbols}
        self.assertEqual(set(found), {"999961.SZ", "999962.SZ"})
        self.assertFalse(found["999961.SZ"]["one_word"])
        self.assertTrue(found["999962.SZ"]["one_word"])

    def test_a_symbol_in_two_sources_is_emitted_once_with_both_evidences(self) -> None:
        # (as_of_date, symbol) is unique, so overlapping sources must merge
        # rather than each insert their own row.
        from app.watchlist_candidate_proposals import materialize_watchlist_proposals
        stamp = datetime(2099, 7, 2, tzinfo=timezone.utc)
        with db.transaction() as connection:
            connection.execute(
                """INSERT INTO quant.disclosure_schedule(symbol,period,provider,pre_date,available_at)
                   VALUES(%s,%s,'test',%s,%s) ON CONFLICT DO NOTHING""",
                ("999961.SZ", date(2098, 12, 31), date(2099, 7, 3), stamp))
            connection.execute(
                """INSERT INTO quant.market_trade_calendar(exchange,calendar_date,is_open,provider,available_at)
                   VALUES('SSE',%s,true,'test',%s) ON CONFLICT DO NOTHING""",
                (date(2099, 7, 3), stamp))
            summary = materialize_watchlist_proposals(connection, self.as_of_date)
            rows = connection.execute(
                """SELECT symbol, count(*) n FROM quant.strategy_watchlist_proposals
                    WHERE as_of_date=%s GROUP BY symbol HAVING count(*) > 1""",
                (self.as_of_date,)).fetchall()
            connection.execute(
                "DELETE FROM quant.disclosure_schedule WHERE symbol='999961.SZ' AND period=%s",
                (date(2098, 12, 31),))
            connection.execute("DELETE FROM quant.market_trade_calendar WHERE calendar_date=%s",
                               (date(2099, 7, 3),))
        self.assertEqual(rows, [], "no symbol may be proposed twice for one session")
        self.assertGreaterEqual(summary["limit_up_continuation"], 1)

    def test_proposals_are_stored_as_an_unscored_universe_source(self) -> None:
        with db.transaction() as connection:
            result = materialize_limit_up_continuation(connection, self.as_of_date)
        selected = {item["symbol"] for item in result["selected"]}
        self.assertTrue({"999961.SZ", "999962.SZ"} <= selected)
        self.assertNotIn("999963.SZ", selected)


if __name__ == "__main__":
    unittest.main()
