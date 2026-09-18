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

The batch path is pinned here too, for the same property one table further
down.  ``daily_bar_batch_repository.upsert_daily_bars`` writes the whole
cross-section in one statement per table, so no caller can give it an order
from outside: its own ``quant.market_bars_daily`` and
``quant.canonical_bars_daily`` statements are ``ON CONFLICT DO UPDATE`` on
``(symbol, trading_date)`` -- the very key ``in_instrument_lock_order`` cites
as its reason for carrying ``trading_date`` -- and both must send their rows
ascending rather than in provider payload order.

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


def _called_name(node: ast.AST) -> str | None:
    """The name a call site uses, whether bare or module-qualified.

    ``upsert_bar(connection, bar)`` parses as ``ast.Call`` over an
    ``ast.Name``; ``daily_bar_repository.upsert_daily_bar(connection, bar)``
    -- this repository's dominant ``from . import module`` style, used by
    ``main``, ``public_market_repository`` and ``tushare_normalization``
    themselves -- parses as ``ast.Call`` over an ``ast.Attribute`` and was
    invisible to the structural check, which is exactly the regression that
    check claims to fail on.  The attribute's OWNER is deliberately not
    examined: ``repo.upsert_daily_bar`` and ``self._upsert.upsert_daily_bar``
    are the same one-bar contract, and a guard that insists on one spelling
    of the owner is the same hole one level down.
    """
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _per_bar_loops(tree: ast.AST) -> list[tuple[int, bool]]:
    """``(line, iterates the shared helper)`` for every per-bar writer loop."""
    found: list[tuple[int, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.AsyncFor)):
            continue
        writes_one_bar = any(
            _called_name(inner) in PER_BAR_WRITERS
            for statement in node.body for inner in ast.walk(statement)
        )
        if not writes_one_bar:
            continue
        found.append((node.lineno, _called_name(node.iter) == "in_instrument_lock_order"))
    return found


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
            for line, ordered in _per_bar_loops(tree):
                loops += 1
                if not ordered:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT).as_posix()}:{line}: "
                        "loop drives a one-bar writer over an unordered iterable"
                    )
        # The offenders first, deliberately.  A fifth caller that forgot the
        # order trips BOTH assertions, and the one that runs first is the one
        # whose message the author reads: "sort this loop, here" is the
        # actionable half, where the count is only "something changed".
        self.assertEqual(offenders, [], "\n".join([
            "",
            "A loop that calls upsert_bar/upsert_daily_bar once per bar owns that",
            "transaction's quant.instruments lock order, because the statement inside",
            "sorts one row.  Iterate daily_bar_repository.in_instrument_lock_order(...)",
            "instead of the raw payload.",
            *offenders,
        ]))
        self.assertEqual(loops, 4, "the set of per-bar loops changed -- re-read this file's docstring")


class TheStructuralCheckSeesBothCallStylesTests(unittest.TestCase):
    """A fifth caller written the way this repository writes modules.

    The check recognised a per-bar writer only as a bare ``ast.Name``, so
    ``from . import daily_bar_repository`` plus
    ``daily_bar_repository.upsert_daily_bar(connection, bar)`` -- the style
    of most modules here -- passed straight through it: the loop was never
    even counted, let alone judged.  Both halves (the writer call and the
    iterable) are checked in both spellings here, with the unordered
    variants as their own negative controls.
    """

    @staticmethod
    def _loops(source: str) -> list[tuple[int, bool]]:
        return _per_bar_loops(ast.parse(source))

    def test_a_bare_call_over_the_helper_is_counted_and_accepted(self) -> None:
        self.assertEqual(self._loops(
            "for bar in in_instrument_lock_order(bars):\n"
            "    upsert_bar(connection, bar)\n"
        ), [(1, True)])

    def test_a_module_qualified_writer_over_the_helper_is_counted_and_accepted(self) -> None:
        self.assertEqual(self._loops(
            "for bar in daily_bar_repository.in_instrument_lock_order(bars):\n"
            "    daily_bar_repository.upsert_daily_bar(connection, bar)\n"
        ), [(1, True)])

    def test_a_module_qualified_writer_over_a_raw_payload_is_reported(self) -> None:
        """The regression the check exists for, in the spelling that hid it."""
        self.assertEqual(self._loops(
            "for bar in bars:\n"
            "    daily_bar_repository.upsert_daily_bar(connection, bar)\n"
        ), [(1, False)])

    def test_a_python_side_sort_bound_to_a_name_is_still_reported(self) -> None:
        """``ordered = in_instrument_lock_order(bars)`` then ``for bar in ordered``.

        Correct code, reported anyway: the check reads the iterable at the
        loop, and a name says nothing about what produced it.  Recorded here
        rather than left as a surprise -- write the call in the ``for``, or
        widen this check deliberately.
        """
        self.assertEqual(self._loops(
            "ordered = in_instrument_lock_order(bars)\n"
            "for bar in ordered:\n"
            "    upsert_bar(connection, bar)\n"
        ), [(2, False)])

    def test_a_loop_that_writes_no_bar_is_not_a_per_bar_loop(self) -> None:
        self.assertEqual(self._loops(
            "for bar in bars:\n"
            "    upsert_daily_bars(connection, [bar])\n"
        ), [])
        self.assertEqual(self._loops(
            "for symbol in symbols:\n"
            "    ensure_instruments(connection, [symbol], 'x')\n"
        ), [])

    def test_the_four_callers_are_all_recognised_in_their_real_source(self) -> None:
        """The predicate is exercised against the files, not only fixtures."""
        counted = 0
        for name in ("main.py", "public_market_repository.py", "tushare_normalization.py"):
            tree = ast.parse((APP_ROOT / name).read_text(encoding="utf-8"))
            loops = _per_bar_loops(tree)
            self.assertTrue(loops, name)
            self.assertTrue(all(ordered for _line, ordered in loops), name)
            counted += len(loops)
        self.assertEqual(counted, 4)


class _BatchConnection:
    """Enough of a psycopg connection for ``upsert_daily_bars`` to run dry.

    It answers the three pre-reads with whatever the test seeded, hands back
    one observation id per input row, and records every statement with its
    parameters so the ORDER of the arrays can be asserted.
    """

    def __init__(self, rows: int, canonical: dict[Any, dict[str, Any]] | None = None) -> None:
        self.rows = rows
        self.canonical = canonical or {}
        self.executed: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = ()) -> "_BatchResult":
        flat = " ".join(sql.split())
        self.executed.append((flat, params))
        if "FROM quant.instruments WHERE symbol=ANY" in flat:
            return _BatchResult([])
        if "FROM quant.canonical_bars_daily c" in flat:
            symbols, dates = params
            return _BatchResult([
                dict(self.canonical[(symbol, trading_date)], symbol=symbol, trading_date=trading_date)
                for symbol, trading_date in zip(symbols, dates)
                if (symbol, trading_date) in self.canonical
            ])
        if "FROM quant.market_bars_daily m" in flat:
            return _BatchResult([])
        if "INSERT INTO quant.raw_market_observations" in flat:
            return _BatchResult([
                {"row_index": index,
                 "observation_id": f"00000000-0000-0000-0000-{index:012d}"}
                for index in range(self.rows)
            ])
        return _BatchResult([])

    def statement(self, fingerprint: str) -> tuple[str, Any]:
        matches = [entry for entry in self.executed if fingerprint in entry[0]]
        if len(matches) != 1:
            raise AssertionError(f"{fingerprint!r} matched {len(matches)} statements")
        return matches[0]


class _BatchResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class BatchStatementLockOrderTests(unittest.TestCase):
    """``daily_bar_batch_repository`` -- the set-based path's own lock order.

    ``upsert_daily_bars`` sorted its ``quant.instruments`` array and left the
    two tables that ``in_instrument_lock_order``'s docstring names as the
    reason its key carries ``trading_date`` -- ``market_bars_daily`` and
    ``canonical_bars_daily`` -- in provider payload order.  Both statements
    are ``ON CONFLICT DO UPDATE`` on ``(symbol, trading_date)``, so both
    row-lock every existing row they touch.  A caller cannot fix this from
    outside: one call writes the whole cross-section.
    """

    def _run(self, canonical: dict[Any, dict[str, Any]] | None = None) -> _BatchConnection:
        from app.daily_bar_batch_repository import upsert_daily_bars

        bars = _bars()
        connection = _BatchConnection(len(bars), canonical)
        self.assertEqual(upsert_daily_bars(connection, bars), len(EXPECTED))
        return connection

    def test_the_instruments_array_stays_ascending(self) -> None:
        connection = self._run()
        _sql, params = connection.statement("INSERT INTO quant.instruments")
        self.assertEqual(params[0], sorted({symbol for symbol, _date in EXPECTED}))

    def test_market_bars_are_written_in_symbol_then_date_order(self) -> None:
        connection = self._run()
        _sql, params = connection.statement("INSERT INTO quant.market_bars_daily")
        self.assertEqual(list(zip(params[0], params[1])), EXPECTED)

    def test_canonical_bars_are_written_in_symbol_then_date_order(self) -> None:
        connection = self._run()
        _sql, params = connection.statement("INSERT INTO quant.canonical_bars_daily")
        self.assertEqual(list(zip(params[0], params[1])), EXPECTED)

    def test_the_evidence_only_update_takes_the_same_order(self) -> None:
        """The second canonical statement locks the same rows and must agree.

        Every incoming bar loses to the provider already on record here, so
        only ``source_observation_ids`` grows -- through an ``UPDATE ... FROM
        unnest(...)``, which locks the rows it matches just as the insert
        does.
        """
        canonical = {
            key: {"close": Decimal("10"), "selected_provider": "tushare",
                  "source_observation_ids": [], "adj_factor": None, "is_suspended": False,
                  "limit_up": None, "limit_down": None}
            for key in EXPECTED
        }
        connection = self._run(canonical)
        with self.assertRaises(AssertionError):
            connection.statement("INSERT INTO quant.canonical_bars_daily")
        _sql, params = connection.statement("UPDATE quant.canonical_bars_daily")
        self.assertEqual(list(zip(params[0], params[1])), EXPECTED)

    def test_the_stored_row_still_follows_the_payload_not_the_sort(self) -> None:
        """A lock-order fix may not change what is stored.

        The two bars of ``600000.SH`` differ, and last-write-wins is by
        payload position, not by sorted position -- so the row for
        ``2026-08-11`` must still carry the close its LAST payload entry
        had, wherever the sort moved it.
        """
        from app.daily_bar_batch_repository import upsert_daily_bars

        bars = _bars()
        bars.append(DailyBar(symbol="600000.SH", trading_date=date(2026, 8, 11),
                             close=Decimal("99"), source="tushare_super_get"))
        connection = _BatchConnection(len(bars))
        upsert_daily_bars(connection, bars)
        _sql, params = connection.statement("INSERT INTO quant.market_bars_daily")
        keys = list(zip(params[0], params[1]))
        self.assertEqual(keys, EXPECTED)
        self.assertEqual(params[5][keys.index(("600000.SH", date(2026, 8, 11)))], Decimal("99"))


if __name__ == "__main__":  # pragma: no cover - direct execution convenience
    unittest.main()
