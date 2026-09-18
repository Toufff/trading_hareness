"""The lock order that ``upsert_daily_bar``'s own statement cannot own.

``daily_bar_repository.upsert_daily_bar`` writes ONE bar per call.  Its
``INSERT ... SELECT FROM unnest(...) ORDER BY 1`` therefore sorts a single
row and decides nothing: when a caller drives it over a multi-symbol payload
inside one transaction, the order that transaction takes its
``quant.instruments`` row locks in is the order of the caller's loop.  And
this is the strongest lock class on the table -- ``ON CONFLICT DO UPDATE``
row-locks every existing conflicting row, the whole cross-section on any day
after the first.

``tests/test_instrument_writer_lock_order.py`` cannot see this: its loop
check is lexical, and the statement lives in ``daily_bar_repository`` while
the loops live in four other places.  So the four callers are pinned here,
two ways:

* behaviourally, one test per caller, recording the symbol order handed to
  ``upsert_bar``; and
* structurally, a walk of ``quant-service/app`` that fails on ANY loop over
  bars feeding ``upsert_bar``/``upsert_daily_bar`` whose iterable is not
  ``in_instrument_lock_order(...)`` -- so a FIFTH caller fails on the day it
  is written rather than being found by the next review round.

Each caller sorts rather than hoisting one
``instrument_registry.ensure_instruments`` call to the top of its
transaction, and the reason is recorded in ``in_instrument_lock_order``'s
docstring: that primitive is ``ON CONFLICT DO NOTHING`` over symbol +
exchange + source, so hoisting would stop maintaining ``industry``, the
three-valued ``is_st``, and the ``exchange``/``source``/``updated_at``
refresh the ``DO UPDATE`` performs.  A lock-order fix is not allowed to
change what is stored.
"""

from __future__ import annotations

import ast
import unittest
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from app.daily_bar_repository import in_instrument_lock_order
from app.public_market_repository import persist_free_daily
from app.request_models import DailyBar
from app.tushare_normalization import normalize_rows

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The functions whose per-call contract is "one bar", so a loop over them is
#: a lock order.  ``upsert_bar`` is ``main.py``'s compatibility forwarder and
#: the name the two injected callables carry at their call sites.
PER_BAR_WRITERS = {"upsert_bar", "upsert_daily_bar"}

#: Deliberately unsorted, and deliberately not merely reversed: the payload
#: order has to be one no sort would produce by accident, and it has to carry
#: two dates of one symbol so the secondary key is exercised too.
UNSORTED = [
    ("600000.SH", date(2026, 8, 11)),
    ("000001.SZ", date(2026, 8, 11)),
    ("600000.SH", date(2026, 8, 10)),
    ("300750.SZ", date(2026, 8, 11)),
    ("000001.SZ", date(2026, 8, 10)),
]
EXPECTED = [
    ("000001.SZ", date(2026, 8, 10)),
    ("000001.SZ", date(2026, 8, 11)),
    ("300750.SZ", date(2026, 8, 11)),
    ("600000.SH", date(2026, 8, 10)),
    ("600000.SH", date(2026, 8, 11)),
]


def _bars() -> list[DailyBar]:
    return [
        DailyBar(symbol=symbol, trading_date=trading_date, close=Decimal("10"),
                 source="tushare_super_get")
        for symbol, trading_date in UNSORTED
    ]


def _keys(call_args_list: Any) -> list[tuple[str, date]]:
    return [(call.args[1].symbol, call.args[1].trading_date) for call in call_args_list]


class InLockOrderHelperTests(unittest.TestCase):
    def test_the_key_is_symbol_then_trading_date(self) -> None:
        ordered = in_instrument_lock_order(_bars())
        self.assertEqual([(bar.symbol, bar.trading_date) for bar in ordered], EXPECTED)

    def test_duplicate_keys_keep_payload_order_so_last_write_still_wins(self) -> None:
        """The sort must not reorder two rows with the same conflict key.

        ``upsert_daily_bar`` is last-write-wins per ``(symbol, trading_date)``;
        an unstable or three-key sort would silently change which provider row
        survives a payload that repeats a key.
        """
        first = DailyBar(symbol="600000.SH", trading_date=date(2026, 8, 10),
                         close=Decimal("10"), source="first")
        second = DailyBar(symbol="600000.SH", trading_date=date(2026, 8, 10),
                          close=Decimal("11"), source="second")
        other = DailyBar(symbol="000001.SZ", trading_date=date(2026, 8, 10),
                         close=Decimal("9"), source="other")
        ordered = in_instrument_lock_order([first, second, other])
        self.assertEqual([bar.source for bar in ordered], ["other", "first", "second"])

    def test_an_empty_payload_is_not_a_special_case(self) -> None:
        self.assertEqual(in_instrument_lock_order([]), [])


class PersistDailyBarBatchLockOrderTests(unittest.TestCase):
    """``main.persist_daily_bar_batch`` -- the licensed per-symbol endpoint."""

    def test_bars_are_written_in_ascending_symbol_and_date_order(self) -> None:
        from app.main import persist_daily_bar_batch

        connection = MagicMock()
        transaction = MagicMock()
        transaction.__enter__.return_value = connection
        with patch("app.main.db.transaction", return_value=transaction), \
             patch("app.main.upsert_bar") as upsert:
            stored = persist_daily_bar_batch(_bars())
        self.assertEqual(stored, len(UNSORTED))
        self.assertEqual(_keys(upsert.call_args_list), EXPECTED)


class ImportBarsLockOrderTests(unittest.TestCase):
    """``main.import_bars`` -- the operator/offline import entrypoint."""

    def test_operator_payload_order_does_not_decide_the_lock_order(self) -> None:
        from app.main import import_bars
        from app.request_models import BarsImport

        connection = MagicMock()
        transaction = MagicMock()
        transaction.__enter__.return_value = connection
        with patch("app.main.db.transaction", return_value=transaction), \
             patch("app.main.upsert_bar") as upsert:
            result = import_bars(BarsImport(bars=_bars()))
        self.assertEqual(result, {"imported": len(UNSORTED)})
        self.assertEqual(_keys(upsert.call_args_list), EXPECTED)


@dataclass
class _CapturedBar:
    symbol: str
    trading_date: date
    close: Any = None
    open: Any = None
    high: Any = None
    low: Any = None
    volume: Any = None
    amount: Any = None
    source: str = "akshare"
    available_at: Any = None


class _RecordingConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = ()) -> "_RecordingConnection":
        self.executed.append((sql, params))
        return self

    def fetchone(self) -> None:
        return None


class _RecordingDatabase:
    def __init__(self) -> None:
        self.connection = _RecordingConnection()

    def transaction(self) -> Any:
        connection = self.connection

        class _Transaction:
            def __enter__(self) -> _RecordingConnection:
                return connection

            def __exit__(self, *_args: Any) -> None:
                return None

        return _Transaction()


class PersistFreeDailyLockOrderTests(unittest.TestCase):
    """``public_market_repository.persist_free_daily`` -- the free providers."""

    def test_free_provider_row_order_does_not_decide_the_lock_order(self) -> None:
        recorded: list[tuple[str, date]] = []

        def upsert_bar(_connection: Any, bar: Any) -> None:
            recorded.append((bar.symbol, bar.trading_date))

        db = _RecordingDatabase()
        stored = persist_free_daily(
            db, "akshare",
            [{"ts_code": symbol, "trade_date": trading_date.strftime("%Y%m%d"), "close": "10.5"}
             for symbol, trading_date in UNSORTED],
            daily_bar_type=_CapturedBar,
            parse_trade_date=lambda value: datetime.strptime(str(value), "%Y%m%d").date(),
            decimal_or_none=lambda value: None if value in (None, "") else Decimal(str(value)),
            upsert_bar=upsert_bar,
            persist_raw_observations=lambda *_a, **_k: 0,
            observed_at=datetime(2026, 9, 1, 7, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(stored, len(UNSORTED))
        self.assertEqual(recorded, EXPECTED)


class TushareFallbackLockOrderTests(unittest.TestCase):
    """``tushare_normalization``'s degraded per-row path.

    The batch statement it falls back from registers the whole cross-section
    in ONE sorted statement, so the fallback must take the same order rather
    than the provider's -- otherwise the very transaction that already lost
    its batching also loses its lock order.
    """

    def test_the_per_row_fallback_writes_in_ascending_order(self) -> None:
        recorded: list[tuple[str, date]] = []

        def upsert_bar(_connection: Any, bar: Any) -> None:
            recorded.append((bar.symbol, bar.trading_date))

        connection = _RecordingConnection()
        rows = [{"ts_code": symbol, "trade_date": trading_date.strftime("%Y%m%d"),
                 "close": "10.5", "vol": "1000", "amount": "500"}
                for symbol, trading_date in UNSORTED]
        with patch("app.tushare_normalization.upsert_daily_bars",
                   side_effect=RuntimeError("batch failed client-side")):
            normalized = normalize_rows(
                connection, "daily", rows, datetime(2026, 8, 20, tzinfo=timezone.utc),
                core_apis=frozenset({"daily", "index_daily", "adj_factor"}),
                date_parser=lambda value: datetime.strptime(str(value), "%Y%m%d").date(),
                exchange_for=lambda symbol: symbol.rsplit(".", 1)[1],
                is_st_security_name=lambda _name: False,
                ensure_instruments=lambda *_args: None,
                upsert_bar=upsert_bar,
                daily_bar_type=DailyBar,
                decimal_or_none=lambda value: Decimal(str(value)) if value not in (None, "") else None,
                safe_error_detail=lambda message, limit: message[:limit],
            )
        self.assertEqual(normalized, len(UNSORTED))
        self.assertEqual(recorded, EXPECTED)


class NoFifthCallerEscapesTheOrderTests(unittest.TestCase):
    """The structural half: a new per-bar loop fails on the day it is written.

    The four tests above pin the four callers that exist.  This one pins the
    rule, so caller number five does not have to be found by a review round
    the way these four were.
    """

    def test_every_per_bar_loop_under_app_iterates_the_shared_helper(self) -> None:
        offenders: list[str] = []
        loops = 0
        for path in sorted(APP_ROOT.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.For, ast.AsyncFor)):
                    continue
                writes_one_bar = any(
                    isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                    and inner.func.id in PER_BAR_WRITERS
                    for statement in node.body for inner in ast.walk(statement)
                )
                if not writes_one_bar:
                    continue
                loops += 1
                iterated = node.iter
                ordered = (
                    isinstance(iterated, ast.Call) and isinstance(iterated.func, ast.Name)
                    and iterated.func.id == "in_instrument_lock_order"
                )
                if not ordered:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}: "
                        "loop drives a one-bar writer over an unordered iterable"
                    )
        self.assertEqual(loops, 4, "the set of per-bar loops changed -- re-read this file's docstring")
        self.assertEqual(offenders, [], "\n".join([
            "",
            "A loop that calls upsert_bar/upsert_daily_bar once per bar owns that",
            "transaction's quant.instruments lock order, because the statement inside",
            "sorts one row.  Iterate daily_bar_repository.in_instrument_lock_order(...)",
            "instead of the raw payload.",
            *offenders,
        ]))


if __name__ == "__main__":  # pragma: no cover - direct execution convenience
    unittest.main()
