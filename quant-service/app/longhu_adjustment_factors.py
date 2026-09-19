"""Cumulative adjustment factors derived from the licensed longhu daily kline.

``quant.canonical_bars_daily.adj_factor`` is the tushare-convention CUMULATIVE
back-adjust factor: it grows at every corporate action and ``close *
adj_factor`` is comparable across dates.  tushare is no longer configured on the
owner host, so this module continues that same series from longhu evidence
only -- no tushare call anywhere on this path.

Evidence, all from ONE licensed call per symbol (``GetKLineDay_W14``):

* ``y[i][1]`` -- the vendor's FORWARD-adjusted (qfq) close, rebased on the
  latest bar.  Measured 2026-09-19: ``Is_FS`` is ignored (0, 1, 2 and absent
  all return the same qfq series), and the adjustment is SUBTRACTIVE for cash
  (``qfq = (raw - cash) / (1 + shares)``), not multiplicative.  A qfq/raw ratio
  therefore drifts with the price inside one segment, so a bare "ratio of
  ratios" shows phantom steps of up to ``dividend yield x daily move`` on
  ordinary days.  It is kept as a cross-check, never as the primary signal.
* ``CQ[i]`` -- the vendor's own corporate-action record on the ex-date:
  ``"<bonus+transfer shares per 10>,<f1>,<f2>,<cash per 10>"``.  With the
  previous raw close it reproduces the exchange ex-rights reference price
  (``(P - cash/10) / (1 + shares/10)``, rounded to the 0.01 tick): 001388.SZ
  07-17 ``4.8,0,0,5`` -> 22.49, 688399.SH 07-10 ``4.8,0,0,0`` -> 30.22,
  603155.SH 07-16 ``4.5,0,0,3.5`` -> 16.83, exactly the published pre_close.
  ``f1``/``f2`` (rights issue fields) were zero in every sample; a non-zero
  value is flagged and handled with the conventional rights formula.

and from the canonical bar itself:

* ``pre_close`` -- exchange-published on tushare-era bars; on longhu bars it is
  the previous candle of the SAME qfq payload fetched that evening, i.e. the
  ex-rights reference price rounded to a tick.  Either way
  ``previous raw close / pre_close`` is the tushare step on an action day.

Before any step is decided, a vendor bar fetched AFTER a later ex-date (a
backfill: 2026-09-07..09-09 were loaded on 09-10) has that forward adjustment
undone on its close and pre_close (:func:`restore_vendor_bars`); otherwise
the early pre_close move and the real ex-date's record count one dividend
twice.

Rule (:func:`decide_step`), applied to every consecutive pair of canonical
bars of one symbol:

1. a CQ record on the later date is an action.  When the bar's pre_close
   moved by at least one tick the step is ``prev_close / pre_close`` (the
   published price wins; a CQ reference more than a tick away is flagged);
   on a VENDOR bar whose pre_close is within a tick of the CQ reference the
   reference wins (the vendor rounds a half tick down, the exchange up);
   with no pre_close at all it is ``prev_close / cq_reference``; when the
   pre_close did NOT move, the CQ step is taken only if the qfq series itself
   moves resolvably by the same step (``cq_qfq``, a missed signature), else
   1 -- and never when an unrecorded step of the same size was taken in the
   previous few bars (:func:`_refuse_double_count`);
2. no CQ, but ``pre_close`` differs from the previous close by at least one
   tick: a falling step is rejected (corporate actions only raise the
   factor); a rising one is accepted only when the qfq step resolvably
   confirms it (:data:`QFQ_CONFIRM_TOLERANCE`); with no qfq evidence at all it
   is accepted and flagged ``pre_close_only`` -- unless uncovered sessions sit
   between the two bars, where the step is ``unresolved`` and the chain stops;
3. neither: a rising qfq step beyond :data:`QFQ_ONLY_ACTION_THRESHOLD` is
   taken and flagged ``qfq_only``;
4. otherwise the step is exactly ``1`` -- snapped, so ordinary days can never
   drift the cumulative series.

The measured accuracy of this rule against three months of stored tushare
factors is in ``docs/ADJUSTMENT_FACTOR_SEMANTICS.md`` (validation mode of
``scripts/adjustment-factor-maintenance.py``).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .tushare_normalization import (
    CUMULATIVE_FACTOR_SEMANTICS,
    DERIVED_FACTOR_PROVIDER,
    promotable_adjustment_factor,
    promotable_factor_evidence_sql_param,
)

#: Where every derived row says it came from (user rule: the source name must
#: be the real source).  Defined next to the promotion rule it has to satisfy.
PROVIDER_KEY = DERIVED_FACTOR_PROVIDER
SOURCE = "longhuvip:GetKLineDay_W14"
METHOD_VERSION = "longhu_cq_preclose_qfq_v2"

#: A-share price tick.
PRICE_TICK = 0.01
#: Half a tick plus float slack: the most a 0.01-rounded price can be off.
ROUNDING_BOUND = 0.0051
#: A pre_close that differs from the previous close by at least one tick is
#: the exchange saying "ex-date".  0.005 separates 0 from 0.01 robustly.
PRE_CLOSE_SIGNATURE_MIN = 0.005
#: A bar pre_close and a CQ reference price that are within one tick describe
#: the same ex-rights price.
REFERENCE_AGREEMENT_TICKS = 0.0101
#: How closely the (noisy, subtractive) qfq step must confirm a pre_close
#: signature that carries no CQ record, on top of its rounding bound.
QFQ_CONFIRM_TOLERANCE = 0.003
#: A qfq-only step (no CQ, no pre_close signature) must exceed this, on top of
#: three rounding bounds, before it is believed.  Below it, the subtractive
#: drift of an ordinary day explains the move.
QFQ_ONLY_ACTION_THRESHOLD = 0.01
#: Stored precision of a derived cumulative factor.
FACTOR_DECIMALS = 6
#: Largest ``st`` one call asks for (the licensed page size).
MAX_SESSIONS_PER_CALL = 300
#: A derived value and a stored tushare factor on the same bar "agree" within
#: this relative error.  tushare itself stores 3-4 decimals (1.033 -> 1.064
#: for 689009.SH is a step of 1.03001 vs the true 1.02995), so a tighter bound
#: would measure tushare's rounding, not the method.
CHECKPOINT_AGREEMENT = 0.002
#: Validation only: a stored tushare step closer to 1 than this is tushare
#: re-rounding its own value, not a corporate action.
TRUE_ACTION_THRESHOLD = 1e-5

A_SHARE_SQL_PATTERN = r"^[0-9]{6}\.(SH|SZ|BJ)$"


# --------------------------------------------------------------------------
# Pure evidence parsing and the per-step rule
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CorporateAction:
    """One vendor ``CQ`` record, per 10 shares, as published."""

    shares_per10: float
    rights_per10: float
    rights_price: float
    cash_per10: float
    raw: str

    @property
    def has_rights_fields(self) -> bool:
        return bool(self.rights_per10 or self.rights_price)


@dataclass(frozen=True)
class LonghuDay:
    """One dated entry of the vendor payload."""

    trading_date: date
    qfq_close: float | None
    cq: str | None = None


@dataclass(frozen=True)
class BarPoint:
    """One canonical bar: the RAW close and the published pre_close."""

    trading_date: date
    close: float
    pre_close: float | None = None
    #: A session the canonical series is missing but the vendor traded,
    #: reconstructed by :func:`fill_bar_gaps`.  Never written anywhere.
    virtual: bool = False
    #: Vendor (qfq) bars only: the CST date the canonical row was fetched on.
    #: Its close and pre_close are the vendor's forward-adjusted candles AS OF
    #: that date, so every corporate action after the bar and up to this date
    #: is already subtracted from them (:func:`restore_vendor_bars`), and its
    #: pre_close is the vendor's rounding of the ex-rights price, not the
    #: exchange's.  ``None`` for exchange-published bars.
    vendor_fetched_on: date | None = None
    #: Set by :func:`restore_vendor_bars` when close/pre_close were un-adjusted.
    restored: bool = False


@dataclass(frozen=True)
class StepDecision:
    trading_date: date
    previous_date: date
    step: float
    basis: str
    flags: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_action(self) -> bool:
        return self.step != 1.0


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _vendor_date(value: Any) -> date | None:
    digits = "".join(character for character in str(value or "") if character.isdigit())[:8]
    if len(digits) != 8:
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
    except ValueError:
        return None


def parse_corporate_action(text: Any) -> CorporateAction | None:
    """Parse one ``CQ`` cell; empty or all-zero means "no action"."""
    raw = str(text or "").strip()
    if not raw:
        return None
    parts = [_number(value) for value in raw.split(",")]
    if len(parts) < 4 or any(value is None for value in parts[:4]):
        # Something is recorded but in a shape nobody has verified: still an
        # action marker; the caller decides from the prices.
        return CorporateAction(0.0, 0.0, 0.0, 0.0, raw)
    shares, rights, rights_price, cash = (float(value) for value in parts[:4])
    if not any((shares, rights, rights_price, cash)):
        return None
    return CorporateAction(shares, rights, rights_price, cash, raw)


def parse_kline_payload(payload: Mapping[str, Any]) -> dict[date, LonghuDay]:
    """Map one ``GetKLineDay_W14`` payload to ``{date: LonghuDay}``."""
    dates = payload.get("x") or []
    values = payload.get("y") or []
    records = payload.get("CQ") or []
    days: dict[date, LonghuDay] = {}
    if not isinstance(dates, list) or not isinstance(values, list) or len(dates) != len(values):
        return days
    for index, (raw_day, candle) in enumerate(zip(dates, values)):
        trading_date = _vendor_date(raw_day)
        if trading_date is None:
            continue
        close = _number(candle[1]) if isinstance(candle, list) and len(candle) > 1 else None
        cq = records[index] if isinstance(records, list) and index < len(records) else None
        days[trading_date] = LonghuDay(
            trading_date, close if close is not None and close > 0 else None,
            str(cq).strip() or None if cq is not None else None)
    return days


def round_to_tick(value: float) -> float:
    return float(Decimal(repr(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def ex_reference_price(previous_close: float, action: CorporateAction) -> float:
    """Exchange ex-rights reference price from the previous RAW close."""
    shares = action.shares_per10 / 10.0
    rights = action.rights_per10 / 10.0
    numerator = previous_close - action.cash_per10 / 10.0 + action.rights_price * rights
    return round_to_tick(numerator / (1.0 + shares + rights))


def qfq_step(previous: BarPoint, current: BarPoint,
             previous_day: LonghuDay | None, current_day: LonghuDay | None) -> tuple[float | None, float | None]:
    """``(step, rounding tolerance)`` implied by the qfq series, if both days exist."""
    if (previous_day is None or current_day is None or previous_day.qfq_close is None
            or current_day.qfq_close is None or previous.close <= 0 or current.close <= 0):
        return None, None
    ratio_previous = previous_day.qfq_close / previous.close
    ratio_current = current_day.qfq_close / current.close
    if ratio_previous <= 0:
        return None, None
    tolerance = ROUNDING_BOUND / previous_day.qfq_close + ROUNDING_BOUND / current_day.qfq_close
    return ratio_current / ratio_previous, tolerance


def decide_step(
    previous: BarPoint, current: BarPoint,
    previous_day: LonghuDay | None, current_day: LonghuDay | None,
    *, gap_actions: Sequence[str] = (), longhu_available: bool = True,
    sessions_between: int = 0,
) -> StepDecision:
    """The cumulative-factor step from ``previous`` to ``current`` (see module doc).

    ``sessions_between`` is the number of exchange sessions the calendar has
    strictly between the two bars that no bar (real or reconstructed) covers:
    across such a hole the current pre_close refers to a close we do not have.
    """
    prev_close = previous.close
    pre_close = current.pre_close if current.pre_close and current.pre_close > 0 else None
    pc_step = prev_close / pre_close if pre_close else None
    signature = pre_close is not None and abs(prev_close - pre_close) >= PRE_CLOSE_SIGNATURE_MIN
    q_step, q_tolerance = qfq_step(previous, current, previous_day, current_day)
    action = parse_corporate_action(current_day.cq) if current_day is not None else None
    evidence: dict[str, Any] = {
        "prev_close": prev_close, "pre_close": pre_close,
        "prev_qfq": previous_day.qfq_close if previous_day else None,
        "qfq": current_day.qfq_close if current_day else None,
        "qfq_step": _rounded(q_step), "pre_close_step": _rounded(pc_step),
        "cq": current_day.cq if current_day else None,
    }
    flags: list[str] = []
    if not longhu_available:
        flags.append("longhu_missing")

    def done(step: float, basis: str) -> StepDecision:
        return StepDecision(current.trading_date, previous.trading_date, float(step), basis,
                            tuple(flags), evidence)

    if action is None and not gap_actions and sessions_between and signature:
        # A pre_close that moved across sessions we have no close for: the
        # move may be the market, not a corporate action, and nothing here
        # can tell the two apart.  Refuse to guess; the caller stops the chain.
        evidence["sessions_between"] = sessions_between
        flags.append("unresolved")
        return done(1.0, "unresolved_bar_gap")

    if gap_actions:
        # A corporate action on a session the canonical series does not carry
        # (a bar hole between the two).  Its own previous close is unknown, so
        # only the qfq step across the whole gap can describe it.
        evidence["gap_actions"] = list(gap_actions)
        flags.append("action_inside_bar_gap")
        if q_step is not None:
            return done(q_step, "gap_qfq")
        if signature and pc_step is not None:
            return done(pc_step, "gap_pre_close")
        flags.append("unresolved")
        return done(1.0, "gap_unresolved")

    if action is not None:
        if action.has_rights_fields:
            flags.append("rights_fields_unverified")
        if not (action.shares_per10 or action.cash_per10 or action.has_rights_fields):
            # A marker we cannot read: trust the prices.
            flags.append("cq_unparsed")
            if signature and pc_step is not None:
                return done(pc_step, "cq_marker_pre_close")
            if q_step is not None and abs(q_step - 1.0) > (q_tolerance or 0.0):
                return done(q_step, "cq_marker_qfq")
            return done(1.0, "cq_marker_no_move")
        reference = ex_reference_price(prev_close, action)
        evidence["cq_reference"] = reference
        cq_step = prev_close / reference if reference > 0 else None
        evidence["cq_step"] = _rounded(cq_step)
        if signature and pc_step is not None:
            # The published price wins.  Validation 2026-06..08: in every case
            # where the two disagreed (13 of 873 actions -- a CQ that rounds
            # the share ratio, or one that lists only the cash half of a
            # bonus+cash action) the pre_close step was tushare's step exactly.
            if reference > 0 and abs(pre_close - reference) <= REFERENCE_AGREEMENT_TICKS:
                if current.vendor_fetched_on is not None and cq_step is not None:
                    # A vendor pre_close is NOT the published price: it is the
                    # vendor's own rounding of the ex-rights price, and it
                    # rounds a half tick DOWN where the exchange rounds up
                    # (600517.SH 2026-09-17: 4.98 - 0.045 -> vendor 4.93,
                    # exchange 4.94, tushare step 4.98/4.94).  Within a tick
                    # the CQ reference (half-up, the exchange rule) is the
                    # better price.
                    if abs(pre_close - reference) > 1e-9:
                        flags.append("vendor_pre_close_rounding")
                    return done(cq_step, "cq_pre_close")
                return done(pc_step, "cq_pre_close")
            flags.append("cq_reference_disagrees")
            return done(pc_step, "pre_close_over_cq")
        if cq_step is None:
            flags.append("cq_reference_invalid")
            return done(q_step if q_step is not None else 1.0, "cq_invalid")
        if pre_close is None:
            # No published price at all: the vendor record is the evidence.
            if action.has_rights_fields and q_step is not None:
                return done(q_step, "cq_rights_qfq")
            return done(cq_step if cq_step != 1.0 else 1.0, "cq")
        # A CQ record but the price did NOT move.  On exchange-published bars
        # this is the vendor recording a dividend smaller than the tick (tushare
        # kept its factor); on vendor bars it can be a missed signature.  The
        # qfq series decides: IT must move resolvably (a flat qfq series
        # "confirms" nothing -- testing the CQ step against the rounding bound
        # instead let q_step == 1 confirm any dividend under ~0.3%), and by the
        # same amount.
        if (q_step is not None and abs(q_step - 1.0) > (q_tolerance or 0.0)
                and abs(cq_step - 1.0) > (q_tolerance or 0.0)
                and abs(q_step / cq_step - 1.0) <= (q_tolerance or 0.0) + QFQ_CONFIRM_TOLERANCE):
            flags.append("pre_close_missed_action")
            return done(cq_step, "cq_qfq")
        flags.append("cq_without_price_move")
        return done(1.0, "cq_no_move")

    if signature and pc_step is not None:
        if pc_step < 1.0:
            # Dividends, bonus shares and rights issues all RAISE a cumulative
            # back-adjust factor.  A falling step without a vendor action
            # record is a bad close on one side (600664.SH 2026-08-31: a
            # legacy quote close of 9.00 against a real 9.12), not an action.
            flags.append("decreasing_step_rejected")
            return done(1.0, "none")
        if q_step is None:
            flags.append("pre_close_only")
            return done(pc_step, "pre_close_only")
        # No vendor action record: the qfq series must show a move it can
        # actually resolve, of the same size.  A one-tick pre_close wobble on a
        # low-priced stock is inside the qfq rounding bound and is rejected.
        if (abs(q_step - 1.0) > (q_tolerance or 0.0)
                and abs(q_step / pc_step - 1.0) <= (q_tolerance or 0.0) + QFQ_CONFIRM_TOLERANCE):
            return done(pc_step, "pre_close_qfq")
        flags.append("pre_close_signature_rejected_by_qfq")
        return done(1.0, "none")

    if q_step is not None and q_step - 1.0 > QFQ_ONLY_ACTION_THRESHOLD + 3 * (q_tolerance or 0.0):
        flags.append("qfq_only")
        return done(q_step, "qfq_only")
    return done(1.0, "none")


def invert_action(adjusted: float, action: CorporateAction) -> float:
    """Undo one forward (qfq) adjustment: ``raw = qfq * (1 + shares) + cash``."""
    return adjusted * (1.0 + action.shares_per10 / 10.0) + action.cash_per10 / 10.0


def restore_vendor_bars(
    bars: Sequence[BarPoint], longhu: Mapping[date, LonghuDay],
) -> tuple[list[BarPoint], list[dict[str, Any]]]:
    """Undo the forward adjustment a LATE-fetched vendor bar carries.

    A vendor bar holds the qfq candle of its date as the vendor served it on
    ``vendor_fetched_on``: every corporate action dated after the bar and up
    to that day is already subtracted from its close AND its pre_close.  On a
    normal evening the two dates are equal and nothing is subtracted.  A bar
    backfilled later is different -- 2026-09-07..09-09 were loaded on 09-10,
    so 002073.SZ's 09-07 close and pre_close were 5.83 against a raw 09-04
    close of 5.85 (its 0.02 dividend went ex on 09-09): a false pre_close
    signature on 09-07 and, with the real ex-date's CQ, the same dividend
    counted twice.

    Each such action is inverted (latest first, rounded to the tick) on both
    prices.  The restoration is taken only when the vendor's CURRENT series
    agrees: the raw close implied by today's qfq close (every later action
    undone) must be closer to the restored close than to the stored one.
    Returns the bars and one note per bar that was examined.
    """
    if not longhu:
        return list(bars), []
    actions = [(value, parse_corporate_action(longhu[value].cq)) for value in sorted(longhu)]
    actions = [(value, action) for value, action in actions if action is not None]
    if not actions:
        return list(bars), []
    result: list[BarPoint] = []
    notes: list[dict[str, Any]] = []
    for bar in bars:
        fetched = bar.vendor_fetched_on
        applied = [] if fetched is None else [
            (value, action) for value, action in actions if bar.trading_date < value <= fetched]
        if not applied:
            result.append(bar)
            continue
        close = bar.close
        for _value, action in reversed(applied):
            close = invert_action(close, action)
        close = round_to_tick(close)
        day = longhu.get(bar.trading_date)
        implied_raw: float | None = None
        if day is not None and day.qfq_close:
            implied_raw = day.qfq_close
            for _value, action in reversed([item for item in actions if item[0] > bar.trading_date]):
                implied_raw = invert_action(implied_raw, action)
        note = {"trading_date": str(bar.trading_date), "fetched_on": str(fetched),
                "actions": [f"{value}:{action.raw}" for value, action in applied],
                "stored_close": bar.close, "restored_close": close,
                "vendor_implied_raw_close": _rounded(implied_raw, 4)}
        if implied_raw is not None and not abs(close - implied_raw) < abs(bar.close - implied_raw):
            note["restored"] = False
            notes.append(note)
            result.append(bar)
            continue
        pre_close = bar.pre_close
        if pre_close:
            for _value, action in reversed(applied):
                pre_close = invert_action(pre_close, action)
            pre_close = round_to_tick(pre_close)
        note.update({"restored": True, "verified": implied_raw is not None,
                     "stored_pre_close": bar.pre_close, "restored_pre_close": pre_close})
        notes.append(note)
        result.append(BarPoint(bar.trading_date, close, pre_close, bar.virtual,
                               bar.vendor_fetched_on, restored=True))
    return result, notes


def fill_bar_gaps(bars: Sequence[BarPoint], longhu: Mapping[date, LonghuDay]) -> list[BarPoint]:
    """Insert the sessions the vendor traded but the canonical series lacks.

    A canonical hole (2026-09-07 carries 5,035 bars against ~5,140 around it)
    breaks the pre_close evidence of the NEXT bar: its pre_close refers to the
    missing session's close, not to the previous canonical close, so a
    corporate action on it looks wrong and an ordinary day looks like one.
    The missing close is reconstructed from the vendor's own series: the qfq
    close carried into the next canonical bar's frame, with every vendor
    action between the two undone (latest first).  The reconstructed bar has
    no pre_close and is never written; it only makes each step compare
    neighbours that really are neighbours.
    """
    ordered = sorted(bars, key=lambda bar: bar.trading_date)
    if len(ordered) < 2 or not longhu:
        return list(ordered)
    sessions = sorted(longhu)
    filled: list[BarPoint] = [ordered[0]]
    for previous, current in zip(ordered, ordered[1:]):
        missing = [value for value in sessions if previous.trading_date < value < current.trading_date]
        current_day = longhu.get(current.trading_date)
        if missing and current_day is not None and current_day.qfq_close and current.close > 0:
            frame = current.close / current_day.qfq_close
            for value in missing:
                day = longhu[value]
                if not day.qfq_close:
                    continue
                estimate = day.qfq_close * frame
                for later in reversed([item for item in sessions if value < item <= current.trading_date]):
                    action = parse_corporate_action(longhu[later].cq)
                    if action is not None:
                        estimate = invert_action(estimate, action)
                if estimate > 0:
                    filled.append(BarPoint(value, round_to_tick(estimate), None, virtual=True))
        filled.append(current)
    return filled


def _rounded(value: float | None, digits: int = 8) -> float | None:
    return None if value is None else round(value, digits)


def factor_value(value: float) -> Decimal:
    return Decimal(repr(round(value, FACTOR_DECIMALS)))


# --------------------------------------------------------------------------
# Chain derivation for one symbol
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Checkpoint:
    """A stored promotable factor for one bar inside the derivation window."""

    trading_date: date
    adj_factor: float
    provider: str
    factor_semantics: str = ""
    #: The stored numeric exactly as read, so a promoted checkpoint equals its
    #: evidence row bit for bit (a float round trip can drop digits).
    value: Decimal | None = None

    @property
    def exact(self) -> Decimal:
        return self.value if self.value is not None else Decimal(str(self.adj_factor))


@dataclass(frozen=True)
class DerivedFactor:
    symbol: str
    trading_date: date
    adj_factor: Decimal
    provider: str               # DERIVED_FACTOR_PROVIDER, or the checkpoint's own provider
    factor_semantics: str
    decision: StepDecision | None
    anchor: Mapping[str, Any]
    checkpoint: Mapping[str, Any] | None = None

    def evidence_row(self) -> dict[str, Any]:
        """The ``raw`` document stored with a derived factor row."""
        decision = self.decision
        return {
            "ts_code": self.symbol, "trade_date": self.trading_date.strftime("%Y%m%d"),
            "adj_factor": str(self.adj_factor),
            "factor_semantics": CUMULATIVE_FACTOR_SEMANTICS,
            "source": SOURCE, "method": METHOD_VERSION,
            "anchor": dict(self.anchor),
            "step": None if decision is None else decision.step,
            "basis": None if decision is None else decision.basis,
            "flags": [] if decision is None else list(decision.flags),
            "previous_date": None if decision is None else str(decision.previous_date),
            "evidence": {} if decision is None else dict(decision.evidence),
            **({"stored_factor_not_adopted": dict(self.checkpoint)} if self.checkpoint else {}),
        }


@dataclass
class SymbolDerivation:
    symbol: str
    factors: list[DerivedFactor]
    decisions: list[StepDecision]
    checkpoints_compared: list[dict[str, Any]]
    held_reason: str | None = None
    anchor: Mapping[str, Any] | None = None
    #: Dates after an unresolved step: left unwritten on purpose.
    truncated_dates: list[date] = field(default_factory=list)
    #: Late-fetched vendor bars whose forward adjustment was undone.
    restorations: list[dict[str, Any]] = field(default_factory=list)


def derive_symbol(
    symbol: str,
    bars: Sequence[BarPoint],
    longhu: Mapping[date, LonghuDay] | None,
    *,
    anchor_date: date | None,
    anchor_factor: float | None,
    anchor_provider: str | None = None,
    new_listing: bool = False,
    checkpoints: Mapping[date, Checkpoint] | None = None,
    calendar: Sequence[date] = (),
) -> SymbolDerivation:
    """Continue one symbol's cumulative series across ``bars`` (ascending).

    ``bars[0]`` must be the anchor bar when an anchor is given.  A symbol with
    no anchor at all and ``new_listing`` starts at 1.0 on its first bar -- the
    tushare convention for a listing -- and says so in its anchor evidence.
    ``checkpoints`` are stored promotable factors inside the window: the
    derived value is compared with each one (the comparison is returned) and
    the chain CONTINUES FROM THE STORED VALUE, which is never overwritten.

    An ``unresolved`` step (a pre_close move across calendar sessions that no
    bar covers) ends the chain: the rows before it are returned, the dates
    after it are listed in ``truncated_dates`` and stay unwritten, so a later
    run with better evidence picks them up instead of inheriting a guess.
    """
    checkpoints = checkpoints or {}
    ordered = sorted(bars, key=lambda bar: bar.trading_date)
    if anchor_date is not None and anchor_factor is not None and anchor_factor > 0:
        if not ordered or ordered[0].trading_date != anchor_date:
            return SymbolDerivation(symbol, [], [], [], "anchor_bar_missing",
                                    {"date": str(anchor_date), "provider": anchor_provider})
        anchor = {"date": str(anchor_date), "adj_factor": anchor_factor,
                  "provider": anchor_provider, "kind": "stored_factor"}
        factor = float(anchor_factor)
        start = 1
    elif new_listing and ordered:
        anchor = {"date": str(ordered[0].trading_date), "adj_factor": 1.0, "provider": None,
                  "kind": "new_listing_starts_at_one"}
        factor = 1.0
        start = 0
    else:
        return SymbolDerivation(symbol, [], [], [], "no_factor_lineage", None)
    lh = longhu or {}
    longhu_dates = sorted(lh)
    ordered, restorations = restore_vendor_bars(ordered, lh)
    ordered = fill_bar_gaps(ordered, lh)
    open_sessions = sorted(set(calendar))
    factors: list[DerivedFactor] = []
    decisions: list[StepDecision] = []
    compared: list[dict[str, Any]] = []
    truncated: list[date] = []
    #: Recent steps taken WITHOUT a vendor record: (bar index, step).
    unrecorded_steps: list[tuple[int, float]] = []
    #: Since the last ADOPTED stored value (or the anchor): that value, and
    #: the products of the derived steps and of the exchange's price steps.
    last_stored = factor
    derived_product = price_product = 1.0
    prices_complete = True
    for index in range(start, len(ordered)):
        current = ordered[index]
        if truncated:
            if not current.virtual:
                truncated.append(current.trading_date)
            continue
        decision: StepDecision | None = None
        if index > 0:
            previous = ordered[index - 1]
            gap = [lh[value].cq for value in longhu_dates
                   if previous.trading_date < value < current.trading_date
                   and parse_corporate_action(lh[value].cq) is not None]
            # Sessions nobody covers: with vendor evidence, the vendor's own
            # trading days (a suspension has none, so a resumption's pre_close
            # stays valid evidence); without it, the exchange calendar.
            between = sum(1 for value in (longhu_dates if lh else open_sessions)
                          if previous.trading_date < value < current.trading_date)
            decision = decide_step(
                previous, current, lh.get(previous.trading_date), lh.get(current.trading_date),
                gap_actions=gap, longhu_available=bool(lh), sessions_between=between)
            decision = _refuse_double_count(decision, index, unrecorded_steps)
            decisions.append(decision)
            if "unresolved" in decision.flags:
                if not current.virtual:
                    truncated.append(current.trading_date)
                continue
            if decision.is_action and decision.basis in UNRECORDED_ACTION_BASES:
                unrecorded_steps.append((index, decision.step))
            factor = factor * decision.step
            derived_product *= decision.step
            if current.pre_close and current.pre_close > 0:
                price_product *= previous.close / current.pre_close
            else:
                prices_complete = False
        if current.virtual:
            continue
        stored = checkpoints.get(current.trading_date)
        if stored is not None:
            error = factor / stored.adj_factor - 1.0 if stored.adj_factor > 0 else None
            comparison = {
                "symbol": symbol, "trading_date": str(current.trading_date),
                "derived": round(factor, FACTOR_DECIMALS), "stored": stored.adj_factor,
                "provider": stored.provider, "relative_error": _rounded(error),
                "agrees": error is not None and abs(error) <= CHECKPOINT_AGREEMENT,
                "basis": decision.basis if decision else None,
                "adopted": True,
            }
            # Only a foreign stored value is judged; this lane's own earlier
            # rows are what the bars already carry and the chain continues
            # from them unconditionally.
            if (not comparison["agrees"] and stored.provider != PROVIDER_KEY
                    and last_stored > 0 and prices_complete):
                verdict = _checkpoint_price_verdict(
                    stored.adj_factor / last_stored, derived_product, price_product)
                comparison["price_check"] = verdict
                if verdict.get("stored_contradicts_prices"):
                    # A stored factor that jumps where the prices show no such
                    # action (300176.SZ 2026-09-09: tushare 4.5917 -> 5.0878
                    # with 4.59 -> 4.58 on pre_close 4.59) would write a fake
                    # +10.6% into every adjusted window.  The derived value is
                    # written instead; the stored row is kept, untouched, and
                    # the conflict is reported for an operator decision.
                    comparison["adopted"] = False
            compared.append(comparison)
            if comparison["adopted"]:
                factor = last_stored = float(stored.adj_factor)
                derived_product = price_product = 1.0
                prices_complete = True
                factors.append(DerivedFactor(
                    symbol, current.trading_date, stored.exact, stored.provider,
                    stored.factor_semantics, decision, anchor, comparison))
                continue
            factors.append(DerivedFactor(
                symbol, current.trading_date, factor_value(factor), PROVIDER_KEY,
                CUMULATIVE_FACTOR_SEMANTICS, decision, anchor, comparison))
            continue
        factors.append(DerivedFactor(
            symbol, current.trading_date, factor_value(factor), PROVIDER_KEY,
            CUMULATIVE_FACTOR_SEMANTICS, decision, anchor))
    return SymbolDerivation(symbol, factors, decisions, compared, None, anchor, truncated,
                            [note for note in restorations if note.get("restored")])


#: Steps taken on price evidence with NO vendor record on that date.
UNRECORDED_ACTION_BASES = frozenset({"pre_close_qfq", "pre_close_only", "qfq_only"})
#: Steps taken on a vendor record while the pre_close did NOT move.
RECORD_WITHOUT_PRICE_BASES = frozenset({"cq_qfq", "cq"})
#: How many bars back an unrecorded step can be the same action as a record.
DOUBLE_COUNT_LOOKBACK_BARS = 5


def _refuse_double_count(decision: StepDecision, index: int,
                         unrecorded: Sequence[tuple[int, float]]) -> StepDecision:
    """Never count one action twice: once on prices, again on its record.

    A vendor record whose ex-date shows no price move, arriving a few bars
    after a step of the same size that had no record, is that same action (a
    bar that still carried the vendor's forward adjustment moved the pre_close
    early).  The record is then worth exactly 1.
    """
    if decision.basis not in RECORD_WITHOUT_PRICE_BASES or not decision.is_action:
        return decision
    for position, step in reversed(unrecorded):
        if index - position > DOUBLE_COUNT_LOOKBACK_BARS:
            break
        if abs(decision.step / step - 1.0) <= CHECKPOINT_AGREEMENT:
            evidence = {**decision.evidence, "already_counted_step": _rounded(step),
                        "refused_step": _rounded(decision.step)}
            return StepDecision(decision.trading_date, decision.previous_date, 1.0,
                                "cq_already_counted",
                                (*decision.flags, "duplicate_of_earlier_step"), evidence)
    return decision


def _checkpoint_price_verdict(stored_ratio: float, derived_product: float,
                              price_product: float) -> dict[str, Any]:
    """Does a disagreeing stored factor, or the derivation, match the prices?

    All three numbers run from the last ADOPTED stored value (or the anchor)
    to this bar: the stored series' own move, the product of the derived
    steps, and the product of the exchange's statement of each step
    (``previous close / pre_close``).  The stored value contradicts the prices
    when its move is far from theirs while the derived move is within it;
    then, and only then, it is not adopted.
    """
    stored_ok = abs(stored_ratio / price_product - 1.0) <= CHECKPOINT_AGREEMENT
    derived_ok = abs(derived_product / price_product - 1.0) <= CHECKPOINT_AGREEMENT
    return {"stored_move": _rounded(stored_ratio), "derived_move": _rounded(derived_product),
            "price_move": _rounded(price_product),
            "stored_contradicts_prices": derived_ok and not stored_ok}


# --------------------------------------------------------------------------
# Longhu IO
# --------------------------------------------------------------------------

def kline_request(code: str, sessions: int) -> dict[str, Any]:
    return {
        "target": "longhu_history", "path": "/w1/api/index.php",
        "params": {"a": "GetKLineDay_W14", "c": "StockLineData", "apiv": "w40", "StockID": code,
                   "Type": "d", "Is_FS": "1", "st": int(sessions), "Index": 0},
    }


def fetch_symbol(source: Any, symbol: str, sessions: int) -> dict[date, LonghuDay]:
    code = symbol.split(".", 1)[0]
    envelope = source.raw_call(kline_request(code, max(5, min(MAX_SESSIONS_PER_CALL, sessions))))
    days: dict[date, LonghuDay] = {}
    for page in envelope.get("pages") or []:
        payload = page.get("payload") if isinstance(page, Mapping) else None
        if not isinstance(payload, Mapping):
            continue
        if payload.get("errcode") is not None and str(payload.get("errcode")) != "0":
            raise RuntimeError(f"Longhu errcode={payload.get('errcode')} action=GetKLineDay_W14")
        days.update(parse_kline_payload(payload))
    return days


def fetch_longhu_evidence(
    source: Any, sessions_by_symbol: Mapping[str, int], *, workers: int = 16, retries: int = 3,
    retry_pause_seconds: float = 2.0,
) -> tuple[dict[str, dict[date, LonghuDay]], dict[str, str]]:
    """One call per symbol, in parallel.  No artificial throttle (user rule).

    A symbol that failed (the upstream answers a burst with an occasional HTTP
    503) is retried in a later round, after a short pause, up to ``retries``
    times; what still fails is returned in ``errors``.
    """
    import time

    evidence: dict[str, dict[date, LonghuDay]] = {}
    errors: dict[str, str] = {}
    pending = dict(sessions_by_symbol)
    for attempt in range(max(1, retries + 1)):
        if not pending:
            break
        if attempt:
            time.sleep(retry_pause_seconds)
        failed: dict[str, int] = {}
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(pending))),
                                thread_name_prefix="longhu-factors") as pool:
            futures = {pool.submit(fetch_symbol, source, symbol, sessions): symbol
                       for symbol, sessions in pending.items()}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    evidence[symbol] = future.result()
                    errors.pop(symbol, None)
                except Exception as error:  # noqa: BLE001 - one symbol must not end the run
                    errors[symbol] = f"{type(error).__name__}: {str(error)[:160]}"
                    failed[symbol] = pending[symbol]
        pending = failed
    return evidence, errors


# --------------------------------------------------------------------------
# Database reads (plain SELECTs; safe on a read-only connection)
# --------------------------------------------------------------------------

#: Factor provider preference when a bar has more than one promotable row.
PROVIDER_PREFERENCE_SQL = (
    "CASE {alias}.provider WHEN 'tushare_super_sdk' THEN 0 WHEN 'tushare_super_get' THEN 1 "
    "WHEN 'tushare_primary' THEN 2 WHEN '" + PROVIDER_KEY + "' THEN 9 ELSE 5 END")

_OPEN_SESSION_SQL = """EXISTS (SELECT 1 FROM quant.market_trade_calendar calendar
                        WHERE calendar.calendar_date={alias}.trading_date AND calendar.is_open)"""

WINDOW_SYMBOLS_SQL = f"""
WITH window_symbols AS (
    SELECT DISTINCT bar.symbol FROM quant.canonical_bars_daily bar
     WHERE bar.trading_date BETWEEN %(from_date)s AND %(to_date)s
       AND bar.quality_status IN ('fresh','partial')
       AND bar.symbol ~ '{A_SHARE_SQL_PATTERN}'
       AND {_OPEN_SESSION_SQL.format(alias='bar')}
)
SELECT ws.symbol, anchor.trading_date AS anchor_date, anchor.adj_factor AS anchor_factor,
       anchor.provider AS anchor_provider,
       (SELECT min(first_bar.trading_date) FROM quant.canonical_bars_daily first_bar
         WHERE first_bar.symbol = ws.symbol) AS first_bar_date
  FROM window_symbols ws
  LEFT JOIN LATERAL (
       SELECT factor.trading_date, factor.adj_factor, factor.provider
         FROM quant.daily_adjustment_factors factor
        WHERE factor.symbol = ws.symbol AND factor.trading_date < %(from_date)s
          AND factor.adj_factor > 0
          AND {promotable_factor_evidence_sql_param('factor', 'raw')}
          AND EXISTS (SELECT 1 FROM quant.canonical_bars_daily anchor_bar
                       WHERE anchor_bar.symbol = factor.symbol
                         AND anchor_bar.trading_date = factor.trading_date
                         AND anchor_bar.close > 0)
        ORDER BY factor.trading_date DESC,
                 -- the value the bar carries wins, as in CHECKPOINTS_SQL: after
                 -- a reported stored-factor conflict the bar carries the
                 -- derived value and the chain must continue from it
                 (EXISTS (SELECT 1 FROM quant.canonical_bars_daily carried
                           WHERE carried.symbol=factor.symbol AND carried.trading_date=factor.trading_date
                             AND carried.adj_factor=factor.adj_factor)) DESC,
                 {PROVIDER_PREFERENCE_SQL.format(alias='factor')}, factor.available_at DESC
        LIMIT 1) anchor ON TRUE
 ORDER BY ws.symbol"""

#: The canonical provider whose close/pre_close are the vendor's qfq candles.
VENDOR_BAR_PROVIDER = "longhuvip_composite"

#: The anchor row is always read, even on a date the trade calendar does not
#: carry (the calendar starts 2026-01-05; an anchor of a symbol suspended
#: since 2025 would otherwise vanish and the symbol be held forever).
BARS_SQL = f"""
SELECT bar.symbol, bar.trading_date, bar.close, bar.pre_close, bar.adj_factor,
       CASE WHEN bar.selected_provider = '{VENDOR_BAR_PROVIDER}'
            THEN (bar.available_at AT TIME ZONE 'Asia/Shanghai')::date END AS vendor_fetched_on
  FROM quant.canonical_bars_daily bar
  JOIN unnest(%(symbols)s::text[], %(starts)s::date[]) AS wanted(symbol, start_date)
    ON wanted.symbol = bar.symbol
 WHERE bar.trading_date >= wanted.start_date AND bar.trading_date <= %(to_date)s
   AND bar.close > 0
   AND (bar.trading_date = wanted.start_date OR {_OPEN_SESSION_SQL.format(alias='bar')})
 ORDER BY bar.symbol, bar.trading_date"""

CHECKPOINTS_SQL = f"""
SELECT DISTINCT ON (factor.symbol, factor.trading_date)
       factor.symbol, factor.trading_date, factor.adj_factor, factor.provider,
       coalesce(factor.raw->>'factor_semantics','') AS factor_semantics
  FROM quant.daily_adjustment_factors factor
  JOIN unnest(%(symbols)s::text[], %(starts)s::date[]) AS wanted(symbol, start_date)
    ON wanted.symbol = factor.symbol
 WHERE factor.trading_date > wanted.start_date AND factor.trading_date <= %(to_date)s
   AND factor.adj_factor > 0
   AND {promotable_factor_evidence_sql_param('factor', 'raw')}
 ORDER BY factor.symbol, factor.trading_date,
          -- The row the bar already carries wins: tushare routes disagree in
          -- the 5th-7th digit (tushare_primary 6471.278 vs tushare_super_sdk
          -- 6471.28 for 600601.SH 08-31) and swapping one real value for
          -- another would only inject rounding jitter into the series.
          (EXISTS (SELECT 1 FROM quant.canonical_bars_daily bar
                    WHERE bar.symbol=factor.symbol AND bar.trading_date=factor.trading_date
                      AND bar.adj_factor=factor.adj_factor)) DESC,
          {PROVIDER_PREFERENCE_SQL.format(alias='factor')}, factor.available_at DESC"""

MARKET_BARS_SQL = """
SELECT symbol, trading_date, adj_factor FROM quant.market_bars_daily
 WHERE trading_date BETWEEN %(from_date)s AND %(to_date)s AND symbol = ANY(%(symbols)s::text[])"""

OPEN_SESSIONS_SQL = """SELECT DISTINCT calendar_date FROM quant.market_trade_calendar
 WHERE is_open AND calendar_date BETWEEN %s AND %s ORDER BY 1"""


@dataclass
class WindowInputs:
    from_date: date
    to_date: date
    symbols: dict[str, dict[str, Any]]          # symbol -> anchor row
    bars: dict[str, list[BarPoint]]
    current_bar_factor: dict[tuple[str, date], Decimal | None]
    stored: dict[str, dict[date, Checkpoint]]
    market_bar_factor: dict[tuple[str, date], Decimal | None]
    sessions: list[date]


def read_window(connection: Any, from_date: date, to_date: date, *,
                symbols: Iterable[str] | None = None) -> WindowInputs:
    """Everything the derivation needs for one window, in five reads."""
    rows = connection.execute(WINDOW_SYMBOLS_SQL, {"from_date": from_date, "to_date": to_date}).fetchall()
    wanted = set(symbols) if symbols is not None else None
    anchors = {row["symbol"]: dict(row) for row in rows if wanted is None or row["symbol"] in wanted}
    names = sorted(anchors)
    starts = [anchors[name]["anchor_date"] or from_date for name in names]
    bars: dict[str, list[BarPoint]] = {name: [] for name in names}
    current: dict[tuple[str, date], Decimal | None] = {}
    for row in connection.execute(BARS_SQL, {"symbols": names, "starts": starts, "to_date": to_date}).fetchall():
        bars[row["symbol"]].append(BarPoint(
            row["trading_date"], float(row["close"]),
            float(row["pre_close"]) if row["pre_close"] is not None else None,
            vendor_fetched_on=row.get("vendor_fetched_on")))
        current[(row["symbol"], row["trading_date"])] = row["adj_factor"]
    stored: dict[str, dict[date, Checkpoint]] = {}
    for row in connection.execute(CHECKPOINTS_SQL, {"symbols": names, "starts": starts, "to_date": to_date}).fetchall():
        stored.setdefault(row["symbol"], {})[row["trading_date"]] = Checkpoint(
            row["trading_date"], float(row["adj_factor"]), row["provider"], row["factor_semantics"],
            Decimal(str(row["adj_factor"])))
    market = {(row["symbol"], row["trading_date"]): row["adj_factor"] for row in connection.execute(
        MARKET_BARS_SQL, {"from_date": from_date, "to_date": to_date, "symbols": names}).fetchall()}
    earliest = min([value for value in starts if value is not None] or [from_date])
    sessions = [row["calendar_date"] for row in connection.execute(
        OPEN_SESSIONS_SQL, (earliest, max(to_date, earliest))).fetchall()]
    return WindowInputs(from_date, to_date, anchors, bars, current, stored, market, sessions)


def sessions_to_fetch(inputs: WindowInputs, today: date) -> dict[str, int]:
    """``st`` per symbol: open sessions from its anchor to ``today`` plus slack."""
    sessions = inputs.sessions
    result: dict[str, int] = {}
    for symbol, anchor in inputs.symbols.items():
        start = anchor.get("anchor_date") or inputs.from_date
        counted = sum(1 for value in sessions if value >= start)
        # Calendar rows can lag the bars; never ask for less than the bars.
        counted = max(counted, len(inputs.bars.get(symbol) or []))
        counted += max(0, (today - max(sessions[-1] if sessions else start, start)).days)
        result[symbol] = min(MAX_SESSIONS_PER_CALL, counted + 5)
    return result


# --------------------------------------------------------------------------
# Planning: what would be written, and why
# --------------------------------------------------------------------------

@dataclass
class FactorPlan:
    from_date: date
    to_date: date
    write_dates: list[date]
    rows: dict[date, list[DerivedFactor]]
    held: dict[str, str]
    derivations: dict[str, SymbolDerivation]
    fetch_errors: dict[str, str]
    market_bar_factor: dict[tuple[str, date], Decimal | None]
    current_bar_factor: dict[tuple[str, date], Decimal | None]
    #: Per date, the symbols with NO planned row there whose bar must not keep
    #: an unsupported factor (held symbols, and dates after an unresolved step).
    clear: dict[date, list[str]] = field(default_factory=dict)
    truncated: dict[str, list[date]] = field(default_factory=dict)


def build_plan(
    inputs: WindowInputs,
    longhu: Mapping[str, Mapping[date, LonghuDay]],
    *,
    write_dates: Iterable[date],
    fetch_errors: Mapping[str, str] | None = None,
    rederive_derived: bool = True,
    null_only_dates: Iterable[date] = (),
    also_replace: Iterable[tuple[str, date]] = (),
) -> FactorPlan:
    """Derive every symbol and keep the rows that fall on ``write_dates``.

    ``rederive_derived`` decides whether an earlier ``longhu_qfq_derived`` row
    inside the window is a checkpoint (the nightly lane: it keeps what it
    already wrote on dates it is not re-working) or is recomputed (the repair:
    idempotent re-runs recompute the same values from the same anchor).
    tushare rows are ALWAYS checkpoints: a real stored factor is never
    overwritten by a derivation.

    ``null_only_dates`` are extra dates on which a row is written ONLY where
    the bar has no factor at all (the nightly lane closing a hole a failed
    fetch left behind on an otherwise complete date), or where the bar's
    ``(symbol, date)`` is in ``also_replace`` -- a value no evidence row
    carries.
    """
    replace_keys = set(also_replace)
    null_only = set(null_only_dates) - set(write_dates)
    targets = sorted(set(write_dates) | null_only)
    target_set = set(targets)
    rows: dict[date, list[DerivedFactor]] = {value: [] for value in targets}
    clear: dict[date, list[str]] = {value: [] for value in targets}
    truncated: dict[str, list[date]] = {}
    held: dict[str, str] = {}
    derivations: dict[str, SymbolDerivation] = {}
    failed_fetch = dict(fetch_errors or {})
    for symbol, anchor in sorted(inputs.symbols.items()):
        if symbol in failed_fetch:
            # No CQ record and no qfq series: a derivation from the pre_close
            # alone would miss every action without a price signature, and a
            # written row becomes a checkpoint nobody revisits.  Leave the
            # bars NULL; the next run's hole fill retries with evidence.
            held[symbol] = "longhu_fetch_failed"
            for value in targets:
                clear[value].append(symbol)
            continue
        stored = inputs.stored.get(symbol, {})
        checkpoints = {
            value: checkpoint for value, checkpoint in stored.items()
            if checkpoint.provider != PROVIDER_KEY
            or (not rederive_derived and value not in target_set)
        }
        first_bar = anchor.get("first_bar_date")
        new_listing = anchor.get("anchor_date") is None and (
            first_bar is not None and first_bar >= inputs.from_date)
        derivation = derive_symbol(
            symbol, inputs.bars.get(symbol) or [], longhu.get(symbol),
            anchor_date=anchor.get("anchor_date"),
            anchor_factor=float(anchor["anchor_factor"]) if anchor.get("anchor_factor") is not None else None,
            anchor_provider=anchor.get("anchor_provider"),
            new_listing=new_listing, checkpoints=checkpoints, calendar=inputs.sessions)
        derivations[symbol] = derivation
        if derivation.held_reason:
            held[symbol] = derivation.held_reason
            for value in targets:
                clear[value].append(symbol)
            continue
        if derivation.truncated_dates:
            truncated[symbol] = list(derivation.truncated_dates)
            for value in derivation.truncated_dates:
                if value in target_set:
                    clear[value].append(symbol)
        for row in derivation.factors:
            if row.trading_date not in target_set:
                continue
            if (row.trading_date in null_only
                    and inputs.current_bar_factor.get((symbol, row.trading_date)) is not None
                    and (symbol, row.trading_date) not in replace_keys):
                continue
            rows[row.trading_date].append(row)
    return FactorPlan(inputs.from_date, inputs.to_date, targets, rows, held, derivations,
                      dict(fetch_errors or {}), inputs.market_bar_factor, inputs.current_bar_factor,
                      clear, truncated)


def plan_summary(plan: FactorPlan, *, detail_limit: int = 40) -> dict[str, Any]:
    """The dry-run report: counts per date, actions by evidence, disagreements."""
    per_date: list[dict[str, Any]] = []
    for value in plan.write_dates:
        rows = plan.rows.get(value, [])
        counts = {"planned": len(rows), "derived": 0, "from_stored_factor": 0,
                  "fills_null": 0, "replaces_other_value": 0, "unchanged": 0,
                  "market_bar_updates": 0}
        for row in rows:
            counts["derived" if row.provider == PROVIDER_KEY else "from_stored_factor"] += 1
            current = plan.current_bar_factor.get((row.symbol, value))
            if current is None:
                counts["fills_null"] += 1
            elif Decimal(current) == row.adj_factor:
                counts["unchanged"] += 1
            else:
                counts["replaces_other_value"] += 1
            key = (row.symbol, value)
            if key in plan.market_bar_factor and (
                    plan.market_bar_factor[key] is None
                    or Decimal(plan.market_bar_factor[key]) != row.adj_factor):
                counts["market_bar_updates"] += 1
        per_date.append({"trading_date": str(value), **counts})
    actions: dict[str, int] = {}
    flagged: dict[str, int] = {}
    action_samples: list[dict[str, Any]] = []
    write_set = set(plan.write_dates)
    for derivation in plan.derivations.values():
        for decision in derivation.decisions:
            if decision.trading_date not in write_set:
                continue
            for flag in decision.flags:
                flagged[flag] = flagged.get(flag, 0) + 1
            if decision.is_action:
                actions[decision.basis] = actions.get(decision.basis, 0) + 1
                if len(action_samples) < detail_limit:
                    action_samples.append({
                        "symbol": derivation.symbol, "trading_date": str(decision.trading_date),
                        "step": round(decision.step, 6), "basis": decision.basis,
                        "flags": list(decision.flags)})
    compared = [item for derivation in plan.derivations.values()
                for item in derivation.checkpoints_compared
                if item["trading_date"] and date.fromisoformat(item["trading_date"]) in write_set]
    disagreements = [item for item in compared if not item["agrees"]]
    conflicts = [item for item in compared if not item.get("adopted", True)]
    restorations = [{"symbol": derivation.symbol, **note}
                    for derivation in plan.derivations.values() for note in derivation.restorations
                    if date.fromisoformat(note["trading_date"]) in write_set]
    errors = [abs(item["relative_error"]) for item in compared if item["relative_error"] is not None]
    return {
        "from_date": str(plan.from_date), "to_date": str(plan.to_date),
        "write_dates": [str(value) for value in plan.write_dates],
        "symbols": len(plan.derivations), "held_symbols": len(plan.held),
        "held": dict(sorted(plan.held.items())),
        "anchors_missing": sorted(symbol for symbol, reason in plan.held.items()
                                  if reason in {"no_factor_lineage", "anchor_bar_missing"}),
        "new_listings": sorted(symbol for symbol, derivation in plan.derivations.items()
                               if (derivation.anchor or {}).get("kind") == "new_listing_starts_at_one"),
        "truncated_symbols": {symbol: [str(value) for value in dates]
                              for symbol, dates in sorted(plan.truncated.items())},
        "dates": per_date,
        "actions_by_basis": dict(sorted(actions.items())),
        "flags": dict(sorted(flagged.items())),
        "action_samples": action_samples,
        "stored_factor_comparisons": len(compared),
        "stored_factor_disagreements": len(disagreements),
        "stored_factor_max_relative_error": round(max(errors), 8) if errors else None,
        "disagreement_samples": disagreements[:detail_limit],
        # Stored factors NOT written because the prices contradict them: each
        # symbol needs an operator decision (see ADJUSTMENT_FACTOR_SEMANTICS).
        "stored_factor_conflicts": len(conflicts),
        "stored_factor_conflict_symbols": sorted({item["symbol"] for item in conflicts}),
        "stored_factor_conflict_samples": conflicts[:detail_limit],
        # Late-fetched vendor bars whose forward adjustment was undone.
        "vendor_bars_restored": len(restorations),
        "vendor_bars_restored_symbols": len({item["symbol"] for item in restorations}),
        "vendor_bar_restoration_samples": restorations[:detail_limit],
        "longhu_fetch_errors": len(plan.fetch_errors),
        "longhu_fetch_error_samples": dict(list(sorted(plan.fetch_errors.items()))[:10]),
    }


# --------------------------------------------------------------------------
# The guarded writer
# --------------------------------------------------------------------------

UPSERT_DERIVED_SQL = """
INSERT INTO quant.daily_adjustment_factors(symbol,trading_date,adj_factor,provider,available_at,raw)
SELECT t.symbol, %(trading_date)s, t.adj_factor, %(provider)s, %(available_at)s, t.raw::jsonb
  FROM unnest(%(symbols)s::text[], %(factors)s::numeric[], %(raws)s::text[]) AS t(symbol, adj_factor, raw)
ON CONFLICT(symbol,trading_date,provider) DO UPDATE
   SET adj_factor=EXCLUDED.adj_factor, available_at=EXCLUDED.available_at, raw=EXCLUDED.raw
 WHERE quant.daily_adjustment_factors.adj_factor IS DISTINCT FROM EXCLUDED.adj_factor"""

#: What a placeholder says when no row replaced it on its bar in this run.
NOT_REPLACED = "none: no factor promoted onto this bar"

#: Each placeholder names the provider of the row ACTUALLY promoted onto its
#: symbol's bar that date (user rule: a source name is the real source) --
#: a stored tushare checkpoint is not a longhu derivation.
ANNOTATE_PLACEHOLDERS_SQL = """
WITH promoted AS (
    SELECT DISTINCT ON (symbol) symbol, provider
      FROM unnest(%(symbols)s::text[], %(providers)s::text[]) AS t(symbol, provider)
), targets AS (
    SELECT candidate.symbol, coalesce(promoted.provider, %(not_replaced)s::text) AS superseded_by
      FROM (SELECT DISTINCT symbol FROM quant.daily_adjustment_factors
             WHERE trading_date=%(trading_date)s AND provider='longhuvip_composite') candidate
      LEFT JOIN promoted USING (symbol)
)
UPDATE quant.daily_adjustment_factors placeholder
   SET raw = placeholder.raw || jsonb_build_object(
           'superseded_at', now()::text,
           'superseded_reason', 'same_day_identity_only placeholder is never promoted to bar tables',
           'superseded_by', targets.superseded_by)
  FROM targets
 WHERE targets.symbol = placeholder.symbol
   AND placeholder.trading_date=%(trading_date)s AND placeholder.provider='longhuvip_composite'
   AND placeholder.raw->>'factor_semantics'='same_day_identity_only'
   AND placeholder.raw->>'superseded_at' IS NULL"""


def persist_factor_date(
    connection: Any, trading_date: date, rows: Sequence[DerivedFactor], *,
    available_at: datetime, clear_symbols: Iterable[str] = (),
) -> dict[str, int]:
    """Write one trading date's factors in the caller's transaction.

    The only place this lane sets ``adj_factor`` on a bar.  Every value passes
    :func:`promotable_adjustment_factor` for ITS provider first -- a derived
    row declares ``corporate_action_cumulative`` explicitly, a stored tushare
    checkpoint keeps its own provider -- so the semantics guard and the
    "never promote an identity placeholder" rule hold here exactly as they do
    in ``tushare_normalization``.  In the same transaction: the placeholder
    evidence of this date is ANNOTATED (never deleted), and a bar whose symbol
    could not be derived loses a value that no promotable evidence row
    carries (NULL is the honest "not yet known") -- a VALUE check, not "some
    row exists": a placeholder 1 next to a real tushare row is still cleared
    -- so no step ever leaves a bar carrying an unsupported value or a
    window NULLed without its replacement.
    """
    promoted = [row for row in rows if promotable_adjustment_factor(
        {"factor_semantics": row.factor_semantics}, provider_key=row.provider)]
    derived = [row for row in promoted if row.provider == PROVIDER_KEY]
    counts = {"derived_rows": 0, "canonical_updates": 0, "market_updates": 0,
              "placeholders_annotated": 0, "unsupported_cleared": 0,
              "refused": len(rows) - len(promoted)}
    if derived:
        import json

        result = connection.execute(UPSERT_DERIVED_SQL, {
            "trading_date": trading_date, "provider": PROVIDER_KEY, "available_at": available_at,
            "symbols": [row.symbol for row in derived],
            "factors": [row.adj_factor for row in derived],
            "raws": [json.dumps(row.evidence_row(), ensure_ascii=True, default=str) for row in derived],
        })
        counts["derived_rows"] = int(getattr(result, "rowcount", 0) or 0)
    if promoted:
        symbols = [row.symbol for row in promoted]
        values = [row.adj_factor for row in promoted]
        result = connection.execute(
            """UPDATE quant.canonical_bars_daily bar SET adj_factor=t.adj_factor, canonicalized_at=now()
                 FROM unnest(%s::text[], %s::numeric[]) AS t(symbol, adj_factor)
                WHERE bar.symbol=t.symbol AND bar.trading_date=%s
                  AND bar.adj_factor IS DISTINCT FROM t.adj_factor""",
            (symbols, values, trading_date))
        counts["canonical_updates"] = int(getattr(result, "rowcount", 0) or 0)
        result = connection.execute(
            """UPDATE quant.market_bars_daily bar SET adj_factor=t.adj_factor
                 FROM unnest(%s::text[], %s::numeric[]) AS t(symbol, adj_factor)
                WHERE bar.symbol=t.symbol AND bar.trading_date=%s
                  AND bar.adj_factor IS DISTINCT FROM t.adj_factor""",
            (symbols, values, trading_date))
        counts["market_updates"] = int(getattr(result, "rowcount", 0) or 0)
    held = sorted(set(clear_symbols) - {row.symbol for row in promoted})
    if held:
        result = connection.execute(
            f"""UPDATE quant.canonical_bars_daily bar SET adj_factor=NULL, canonicalized_at=now()
                 WHERE bar.trading_date=%s AND bar.symbol = ANY(%s::text[])
                   AND bar.adj_factor IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM quant.daily_adjustment_factors factor
                                    WHERE factor.symbol=bar.symbol AND factor.trading_date=bar.trading_date
                                      AND factor.adj_factor = bar.adj_factor
                                      AND {promotable_factor_evidence_sql_param('factor', 'raw')})""",
            (trading_date, held))
        counts["unsupported_cleared"] = int(getattr(result, "rowcount", 0) or 0)
    result = connection.execute(ANNOTATE_PLACEHOLDERS_SQL, {
        "trading_date": trading_date, "symbols": [row.symbol for row in promoted],
        "providers": [row.provider for row in promoted], "not_replaced": NOT_REPLACED})
    counts["placeholders_annotated"] = int(getattr(result, "rowcount", 0) or 0)
    return counts


# --------------------------------------------------------------------------
# Validation against stored tushare factors (read-only)
# --------------------------------------------------------------------------

VALIDATION_TRUTH_SQL = f"""
SELECT DISTINCT ON (factor.symbol, factor.trading_date)
       factor.symbol, factor.trading_date, factor.adj_factor
  FROM quant.daily_adjustment_factors factor
 WHERE factor.trading_date BETWEEN %(from_date)s AND %(to_date)s
   AND factor.provider LIKE 'tushare%%' AND factor.adj_factor > 0
   AND coalesce(factor.raw->>'factor_semantics','') IN ('', '{CUMULATIVE_FACTOR_SEMANTICS}')
   AND factor.symbol ~ '{A_SHARE_SQL_PATTERN}'
 ORDER BY factor.symbol, factor.trading_date,
          {PROVIDER_PREFERENCE_SQL.format(alias='factor')}, factor.available_at DESC"""

VALIDATION_BARS_SQL = f"""
SELECT bar.symbol, bar.trading_date, bar.close, bar.pre_close
  FROM quant.canonical_bars_daily bar
 WHERE bar.trading_date BETWEEN %(from_date)s AND %(to_date)s AND bar.close > 0
   AND bar.symbol ~ '{A_SHARE_SQL_PATTERN}'
   AND {_OPEN_SESSION_SQL.format(alias='bar')}
 ORDER BY bar.symbol, bar.trading_date"""


def read_validation_inputs(connection: Any, from_date: date, to_date: date) -> tuple[
        dict[str, list[BarPoint]], dict[str, dict[date, float]]]:
    bars: dict[str, list[BarPoint]] = {}
    for row in connection.execute(VALIDATION_BARS_SQL, {"from_date": from_date, "to_date": to_date}).fetchall():
        bars.setdefault(row["symbol"], []).append(BarPoint(
            row["trading_date"], float(row["close"]),
            float(row["pre_close"]) if row["pre_close"] is not None else None))
    truth: dict[str, dict[date, float]] = {}
    for row in connection.execute(VALIDATION_TRUTH_SQL, {"from_date": from_date, "to_date": to_date}).fetchall():
        truth.setdefault(row["symbol"], {})[row["trading_date"]] = float(row["adj_factor"])
    return bars, truth


def validate(
    bars: Mapping[str, Sequence[BarPoint]],
    truth: Mapping[str, Mapping[date, float]],
    longhu: Mapping[str, Mapping[date, LonghuDay]],
    *, action_tolerance: float = CHECKPOINT_AGREEMENT, detail_limit: int = 30,
) -> dict[str, Any]:
    """Compare every derived step, and every symbol's chained end value, with tushare.

    A step pair counts when both bars carry a stored tushare factor.  A true
    action is ``|F_d / F_p - 1| > 1e-6``.  ``matched`` = derived step within
    ``action_tolerance`` of the true one (both for actions and for the exact
    ``1`` of an ordinary day).  A stored step within :data:`TRUE_ACTION_THRESHOLD`
    of 1 is tushare re-rounding its own cumulative value (e.g. 600612.SH
    08-17/08-18: 0.999998 then 1.000002) and is counted as jitter, not as an
    action.  The chained check starts every symbol from its
    first stored factor and carries the DERIVED series to the last date -- the
    error that actually reaches a research window after N sessions.
    """
    pairs = actions = matched_actions = misses = false_actions = wrong_size = 0
    non_actions = exact_non_actions = 0
    max_step_error = 0.0
    by_basis: dict[str, dict[str, int]] = {}
    miss_samples: list[dict[str, Any]] = []
    false_samples: list[dict[str, Any]] = []
    wrong_samples: list[dict[str, Any]] = []
    chain_errors: list[float] = []
    worst_chain: list[tuple[float, str]] = []
    symbols_without_longhu = 0
    jitter = 0
    for symbol, symbol_bars in bars.items():
        factors = truth.get(symbol) or {}
        ordered = [bar for bar in sorted(symbol_bars, key=lambda value: value.trading_date)
                   if bar.trading_date in factors]
        if len(ordered) < 2:
            continue
        lh = longhu.get(symbol) or {}
        if not lh:
            symbols_without_longhu += 1
        dates = sorted(lh)
        chained = factors[ordered[0].trading_date]
        all_bars = sorted(symbol_bars, key=lambda value: value.trading_date)
        index_of = {bar.trading_date: position for position, bar in enumerate(all_bars)}
        for previous, current in zip(ordered, ordered[1:]):
            if index_of[current.trading_date] - index_of[previous.trading_date] != 1:
                # A bar without a stored factor sits between them: not a clean pair.
                chained = factors[current.trading_date]
                continue
            gap = [lh[value].cq for value in dates if previous.trading_date < value < current.trading_date
                   and parse_corporate_action(lh[value].cq) is not None]
            decision = decide_step(previous, current, lh.get(previous.trading_date),
                                   lh.get(current.trading_date), gap_actions=gap,
                                   longhu_available=bool(lh))
            true_step = factors[current.trading_date] / factors[previous.trading_date]
            chained *= decision.step
            pairs += 1
            error = decision.step / true_step - 1.0
            max_step_error = max(max_step_error, abs(error))
            basis = by_basis.setdefault(decision.basis, {"steps": 0, "true_actions": 0, "within_tolerance": 0})
            basis["steps"] += 1
            is_true_action = abs(true_step - 1.0) > TRUE_ACTION_THRESHOLD
            if not is_true_action and abs(true_step - 1.0) > 1e-9:
                jitter += 1
            if is_true_action:
                basis["true_actions"] += 1
            if abs(error) <= action_tolerance:
                basis["within_tolerance"] += 1
            sample = {"symbol": symbol, "trading_date": str(current.trading_date),
                      "true_step": round(true_step, 6), "derived_step": round(decision.step, 6),
                      "basis": decision.basis, "flags": list(decision.flags),
                      "prev_close": previous.close, "pre_close": current.pre_close,
                      "cq": decision.evidence.get("cq"), "qfq_step": decision.evidence.get("qfq_step")}
            if is_true_action:
                actions += 1
                if not decision.is_action:
                    misses += 1
                    if len(miss_samples) < detail_limit:
                        miss_samples.append(sample)
                elif abs(error) <= action_tolerance:
                    matched_actions += 1
                else:
                    wrong_size += 1
                    if len(wrong_samples) < detail_limit:
                        wrong_samples.append(sample)
            else:
                non_actions += 1
                if decision.is_action:
                    false_actions += 1
                    if len(false_samples) < detail_limit:
                        false_samples.append(sample)
                else:
                    exact_non_actions += 1
        last = ordered[-1].trading_date
        chain_error = chained / factors[last] - 1.0
        chain_errors.append(abs(chain_error))
        worst_chain.append((abs(chain_error), symbol))
    chain_errors.sort()
    worst_chain.sort(reverse=True)

    def percentile(values: list[float], share: float) -> float | None:
        if not values:
            return None
        return round(values[min(len(values) - 1, int(math.ceil(share * len(values))) - 1)], 8)

    return {
        "step_pairs": pairs, "true_actions": actions, "matched_actions": matched_actions,
        "action_match_rate": round(matched_actions / actions, 6) if actions else None,
        "missed_actions": misses, "wrong_size_actions": wrong_size,
        "false_actions": false_actions, "non_action_pairs": non_actions,
        "tushare_rounding_jitter_pairs": jitter,
        "non_action_exact": exact_non_actions,
        "max_step_relative_error": round(max_step_error, 8),
        "action_tolerance": action_tolerance,
        "by_basis": dict(sorted(by_basis.items())),
        "chained_symbols": len(chain_errors),
        "chained_error_median": percentile(chain_errors, 0.5),
        "chained_error_p99": percentile(chain_errors, 0.99),
        "chained_error_max": round(chain_errors[-1], 8) if chain_errors else None,
        "chained_symbols_over_tolerance": sum(1 for value in chain_errors if value > action_tolerance),
        "worst_chained": [{"symbol": symbol, "relative_error": round(value, 8)}
                          for value, symbol in worst_chain[:10]],
        "symbols_without_longhu": symbols_without_longhu,
        "miss_samples": miss_samples, "false_action_samples": false_samples,
        "wrong_size_samples": wrong_samples,
    }


__all__ = [
    "BarPoint", "Checkpoint", "CorporateAction", "DerivedFactor", "FactorPlan", "LonghuDay",
    "METHOD_VERSION", "PROVIDER_KEY", "SOURCE", "StepDecision", "SymbolDerivation", "WindowInputs",
    "build_plan", "decide_step", "derive_symbol", "ex_reference_price", "fetch_longhu_evidence",
    "fill_bar_gaps", "invert_action",
    "parse_corporate_action", "parse_kline_payload", "persist_factor_date", "plan_summary",
    "read_validation_inputs", "read_window", "sessions_to_fetch", "validate",
]
