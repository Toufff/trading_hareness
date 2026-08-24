import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
import uuid

from app.intraday_watchlist_scan_service import (
    IntradayWatchlistScanDependencies,
    build_peer_contexts,
    run_watchlist_scan,
)


class IntradayWatchlistScanServiceTests(unittest.TestCase):
    @staticmethod
    def request():
        return SimpleNamespace(symbols=[], realtime_validation_offset=0, realtime_validation_limit=4)

    def test_closed_session_persists_only_terminal_scan(self):
        calls = []

        async def session():
            return False, "SSE is closed"

        async def watches(_):
            return [{"symbol": "000001.SZ"}]

        async def terminal(*args):
            calls.append(args)

        async def unexpected(*_args, **_kwargs):
            raise AssertionError("closed session must not call a market or alert path")

        dependencies = self.dependencies(
            realtime_session=session, load_watches=watches, persist_terminal=terminal,
            prune_rule_inputs=unexpected, retry_pending_alerts=unexpected, load_exact_memberships=unexpected,
            capture_quotes=unexpected, surge_context=unexpected, tushare_minutes=unexpected,
            fast_confirmations=unexpected, board_cache_evidence=unexpected, persist_signals=unexpected,
            deliver_alert=unexpected,
        )
        result = asyncio.run(run_watchlist_scan(self.request(), dependencies))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "SSE is closed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], "blocked")

    def test_active_scan_preserves_exact_membership_and_confirmed_alert_path(self):
        observed_at = datetime(2026, 8, 24, 2, tzinfo=timezone.utc)
        event_id = uuid.uuid4()
        calls = []

        async def session():
            return True, "continuous auction"

        async def watches(_):
            return [{"symbol": "000001.SZ", "metadata": {"surge_strategy": {"enabled": True, "peer_symbols": ["000002.SZ"]}}}]

        async def terminal(*args):
            calls.append(("terminal", args))

        async def prune(value):
            calls.append(("prune", value))

        async def retry():
            return {"retried": 0}

        async def memberships(symbols, value):
            self.assertEqual(symbols, ["000001.SZ"])
            self.assertEqual(value, observed_at)
            return [{"symbol": "000001.SZ", "taxonomy_key": "ths_concept_flow", "sector_key": "c1"}]

        async def capture(symbols, _observed, slo):
            self.assertEqual(symbols, ["000001.SZ"])
            self.assertEqual(slo, 20.0)
            return SimpleNamespace(
                quotes={"000001.SZ": {"price": 10.0}}, tencent_rows=[{"symbol": "000001.SZ"}],
                fresh_watch_rows=[{"symbol": "000001.SZ"}], sina_watch_rows=[], eastmoney_watch_flow_rows=[],
                all_a_snapshot_status={"status": "fresh", "cross_sectional": True}, latency_ms=9,
            )

        async def surge(_watches, *, mapped_peers):
            self.assertIn("000001.SZ", mapped_peers)
            return {"000002.SZ": {"pct_change": 3.0}}, {"provider_status": "completed"}

        async def minutes(symbols):
            self.assertEqual(symbols, ["000001.SZ"])
            return {"000001.SZ": {"source": {"status": "completed"}}}

        async def confirmations(*_):
            return {"000001.SZ": {"status": "confirmed"}}

        async def board(_):
            return {"status": "cached"}

        async def persist(*args):
            calls.append(("persist", args))
            return [{
                "signal_event_id": event_id, "symbol": "000001.SZ", "signal_type": "entry", "severity": "high",
                "state": "confirmed", "watch": {"symbol": "000001.SZ"}, "quote": {"price": 10.0}, "minute": {},
            }]

        async def deliver(received_event_id, text):
            self.assertEqual(received_event_id, event_id)
            self.assertIn("card:000001.SZ", text)
            return {"status": "sent"}

        dependencies = self.dependencies(
            now_utc=lambda: observed_at, realtime_session=session, load_watches=watches, persist_terminal=terminal,
            prune_rule_inputs=prune, retry_pending_alerts=retry, load_exact_memberships=memberships,
            mapped_peers=lambda _symbols, _rows: {"000001.SZ": {"peer_symbols": ["000003.SZ"], "groups": ["c1"]}},
            high_frequency_window=lambda _: True, capture_quotes=capture, surge_context=surge,
            peer_context=lambda peers, _features: {"peer_symbols": peers}, tushare_minutes=minutes,
            fast_confirmations=confirmations, board_cache_evidence=board,
            build_source_status=lambda **kwargs: {"direct": len(kwargs["fresh_watch_rows"])},
            persist_signals=persist, deliver_alert=deliver,
            alert_text=lambda *_args, decision_card_url=None: f"alert {decision_card_url}",
            decision_card_url=lambda symbol: f"card:{symbol}",
        )
        result = asyncio.run(run_watchlist_scan(self.request(), dependencies))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["source_status"], {"direct": 1})
        self.assertEqual(result["alerts"][0]["delivery"]["status"], "sent")
        self.assertNotIn("watch", result["signals"][0])
        self.assertTrue(any(name == "persist" for name, _ in calls))

    @staticmethod
    def dependencies(**overrides):
        async def default_async(*_args, **_kwargs):
            return {}

        values = {
            "now_utc": lambda: datetime(2026, 8, 24, tzinfo=timezone.utc), "new_scan_id": uuid.uuid4,
            "realtime_session": default_async, "load_watches": default_async,
            "watchlist_capacity": lambda _: {"blocked": False}, "persist_terminal": default_async,
            "prune_rule_inputs": default_async, "retry_pending_alerts": default_async,
            "load_exact_memberships": default_async, "mapped_peers": lambda *_: {},
            "high_frequency_window": lambda _: False, "capture_quotes": default_async,
            "surge_context": default_async, "peer_context": lambda *_: {}, "watch_priority_key": lambda row: row["symbol"],
            "realtime_validation_slice": lambda symbols, offset, limit: (symbols[offset:offset + limit], offset + limit),
            "tushare_minutes": default_async, "fast_confirmations": default_async,
            "board_cache_evidence": default_async, "build_source_status": lambda **_: {},
            "persist_signals": default_async, "shadow_pool": default_async,
            "shadow_rotation_due": lambda _: False, "shadow_rotation_slice": lambda *_: ([], 0),
            "capture_shadow_quotes": default_async, "persist_shadow_observations": default_async,
            "persist_shadow_status": default_async,
            "deliver_alert": default_async,
            "alert_text": lambda *_args, **_kwargs: "alert", "decision_card_url": lambda _: None,
        }
        values.update(overrides)
        return IntradayWatchlistScanDependencies(**values)

    def test_peer_contexts_keep_only_exact_and_configured_valid_symbols(self):
        contexts = build_peer_contexts(
            [{"symbol": "000001.SZ", "metadata": {"upside_research": {"enabled": True, "peer_symbols": ["000002.SZ", "bad"]}}}],
            {"000001.SZ": {"peer_symbols": ["000003.SZ"], "groups": ["concept:a"]}},
            {}, lambda peers, _: {"received": peers},
        )
        self.assertEqual(contexts["000001.SZ"]["received"], ["000002.SZ", "000003.SZ"])
        self.assertEqual(contexts["000001.SZ"]["exact_membership_groups"], ["concept:a"])


if __name__ == "__main__":
    unittest.main()
