"""Data-derived per-name exposure caps for discipline plans (replaces the hand-picked stage table).

Policy (fixed by the user): a single name may lose at most ``TOLERANCE_PCT`` = 5% of equity in an
extreme move.  No account-level limit is implied or added here.

Method (chosen by the orchestrator, documented in docs/TRADE_DISCIPLINE.md):

* **Extreme move.**  From the close of session ``t``, the worst low of the symbol's next two actual
  sessions: ``loss = 1 - min(low[t+1], low[t+2]) / close[t]``.  Two sessions capture a locked limit
  down that cannot be exited on the next day.  The statistic is the 99th percentile of that loss.
* **Prices.**  Adjusted research prices (``close * adj_factor``) so an ex-rights day is not read as a
  crash; a sample whose three bars do not all carry a real factor is dropped, never filled with raw
  prices (``research_prices.adjusted_value``).  Suspended rows are not sessions; if the symbol's next
  session is more than ``MAX_GAP_CALENDAR_DAYS`` calendar days away the sample is dropped.
* **Cells.**  stage x board.  The stage is the discipline generator's own classifier
  (``stage.daily_metrics`` + ``stage.classify_stage``) applied to the same raw 60-row window ending at
  ``t`` that ``inputs.settled_daily_bars`` would hand the generator.  The board is the price-limit regime
  from ``market_rules.a_share_limit_ratio`` (main 10%, ChiNext/STAR 20%, BJ 30%, main-board ST 5%);
  the ST flag is point in time, read from the session's own ``limit_down`` band.
* **Cap.**  ``cap_pct = 5 / q99_loss_pct`` rounded down to a multiple of 5, clamped to [5, 50].  A
  cell with fewer than ``MIN_SAMPLES`` samples uses its board pooled across stages (``fallback``).
* **Holiday.**  The same statistic on samples whose next session follows a market closure of at least
  ``HOLIDAY_CLOSURE_DAYS`` calendar days (loss over the first two sessions after reopening); cap
  clamped to [0, 50].  A holiday cap at or above the stage cap means no holiday line.

This module is pure: ``scripts/calibrate-discipline-exposure.py`` feeds it rows read-only from the
database and writes ``exposure_calibration.json``; the generator only reads that artifact.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from ..market_rules import a_share_limit_ratio
from ..research_prices import adjusted_value
from .risk_policy import PER_NAME_LOSS_TOLERANCE_PCT
from .stage import InsufficientBars, classify_stage, daily_metrics, normalize_bars

CALIBRATION_METHOD_VERSION = "discipline-exposure-calibration-v1"
ARTIFACT_PATH = Path(__file__).with_name("exposure_calibration.json")
# The single policy value, shared with the stop-loss lens; see risk_policy.py.
TOLERANCE_PCT = float(PER_NAME_LOSS_TOLERANCE_PCT)
PERCENTILE = 99.0
SECONDARY_PERCENTILE = 95.0
HORIZON_SESSIONS = 2
MAX_GAP_CALENDAR_DAYS = 10
MIN_SAMPLES = 300
CAP_STEP_PCT = 5
CAP_MIN_PCT = 5
CAP_MAX_PCT = 50
HOLIDAY_CAP_MIN_PCT = 0
HOLIDAY_CLOSURE_DAYS = 5
WINDOW_ROWS = 60
ST_BAND_TOLERANCE = 0.01

BOARDS: dict[float, str] = {0.10: "main_10", 0.20: "growth_20", 0.30: "bj_30", 0.05: "st_5"}
BOARD_LABEL: dict[str, str] = {"main_10": "主板（10%）", "growth_20": "创业板/科创板（20%）",
                               "bj_30": "北交所（30%）", "st_5": "主板ST（5%）"}
POOLED = "*"

METHOD_TEXT = (
    "单只股票极端亏损容忍度 5%（用户设定，不设账户总额限制）。极端波动 = 计划日收盘后该股接下来 2 个实际交易日"
    "最低价相对计划日收盘的最大跌幅 1 − min(low[t+1], low[t+2]) / close[t]，取 99% 分位；复权价（close×adj_factor，"
    "缺复权因子的样本剔除，不用原始价代替）；停牌不算交易日，t 与下一交易日相隔超过 10 个自然日的样本剔除。"
    "按 阶段 × 板块 分格：阶段用纪律卡生成器同一分类器作用于截至 t 的同一 60 行原始日线窗口；板块按涨跌幅制度"
    "（主板10%/创业板科创板20%/北交所30%/主板ST 5%，ST 按当日跌停价推断）。样本少于 300 的格改用同板块全部阶段合并，"
    "并标记 fallback。上限 = 5 ÷ 99%分位跌幅(%)，向下取整到 5 的倍数，限制在 [5, 50]。休市线：下一交易日前休市 "
    "≥5 个自然日的样本单独统计复开后前 2 个交易日的同一指标，上限限制在 [0, 50]；不低于阶段上限则不出休市线。"
)


# --------------------------------------------------------------------------
# pure pieces
# --------------------------------------------------------------------------
def quantile(values: list[float], percentile: float) -> float:
    """Linear-interpolation quantile (numpy's default); deterministic for a given multiset."""
    if not values:
        raise ValueError("quantile of an empty sample")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100.0
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def cap_from_q99(q99_loss_pct: float, *, minimum: int = CAP_MIN_PCT, maximum: int = CAP_MAX_PCT) -> int:
    """``5 / q99_pct`` as a percent of equity, rounded down to a multiple of 5, clamped."""
    if q99_loss_pct <= 0:
        return maximum
    raw = TOLERANCE_PCT / q99_loss_pct * 100.0
    stepped = int(math.floor(raw / CAP_STEP_PCT + 1e-9)) * CAP_STEP_PCT
    return max(minimum, min(maximum, stepped))


def st_from_limit_band(symbol: str, pre_close: float | None, limit_down: float | None) -> bool | None:
    """Point-in-time main-board ST flag from the session's own limit-down band; ``None`` when unknown."""
    if pre_close is None or limit_down is None or pre_close <= 0 or limit_down <= 0:
        return None
    band = 1.0 - limit_down / pre_close
    if abs(band - 0.05) <= ST_BAND_TOLERANCE:
        return True
    if abs(band - a_share_limit_ratio(symbol, False)) <= ST_BAND_TOLERANCE:
        return False
    return None


def board_key(symbol: str, is_st: bool | None) -> str:
    """Board by price-limit regime, through the platform's single limit-ratio helper."""
    ratio = a_share_limit_ratio(symbol, is_st=bool(is_st))
    return BOARDS[round(ratio, 2)]


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _day(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def generator_bar(row: dict[str, Any]) -> dict[str, Any]:
    """The bar exactly as ``inputs.settled_daily_bars`` hands it to the generator."""
    return {"trading_date": _day(row["trading_date"]).isoformat(), "open": _num(row.get("open")),
            "high": _num(row.get("high")), "low": _num(row.get("low")), "close": _num(row.get("close")),
            "volume": _num(row.get("volume")), "amount": _num(row.get("amount")),
            "synthetic": False, "forming": False}


def symbol_samples(symbol: str, rows: list[dict[str, Any]], *, current_is_st: bool = False,
                   holiday_resume_dates: frozenset[date] = frozenset()) -> tuple[list[tuple[str, str, float, bool]], dict[str, int]]:
    """Every usable ``(stage, board, loss, is_holiday)`` sample of one symbol's full history.

    ``rows`` are the symbol's canonical rows in ascending date order (all of them, suspended rows
    included, exactly as stored).  ``holiday_resume_dates`` are the market sessions that reopen after
    a closure of at least ``HOLIDAY_CLOSURE_DAYS`` calendar days.
    """
    counts = {"rows": len(rows), "samples": 0, "no_next_sessions": 0, "gap_too_long": 0, "factor_missing": 0,
              "insufficient_window": 0, "st_from_current_flag": 0}
    ordered = sorted(rows, key=lambda row: _day(row["trading_date"]))
    sessions = [index for index, row in enumerate(ordered) if not row.get("is_suspended")]
    session_rank = {index: rank for rank, index in enumerate(sessions)}
    bars = [generator_bar(row) for row in ordered]
    valid = [bool(normalize_bars([bar])) for bar in bars]
    samples: list[tuple[str, str, float, bool]] = []
    for index in sessions:
        rank = session_rank[index]
        if rank + HORIZON_SESSIONS >= len(sessions):
            counts["no_next_sessions"] += 1
            continue
        chain = [ordered[index], ordered[sessions[rank + 1]], ordered[sessions[rank + 2]]]
        days = [_day(row["trading_date"]) for row in chain]
        if (days[1] - days[0]).days > MAX_GAP_CALENDAR_DAYS or (days[2] - days[1]).days > MAX_GAP_CALENDAR_DAYS:
            counts["gap_too_long"] += 1
            continue
        close_t = adjusted_value(chain[0], "close")
        lows = [adjusted_value(row, "low") for row in chain[1:]]
        if close_t is None or close_t <= 0 or any(low is None for low in lows):
            counts["factor_missing"] += 1
            continue
        # The generator's window: the last WINDOW_ROWS stored rows through t (suspended included in the
        # count), then suspended / inconsistent rows dropped - exactly settled_daily_bars + normalize_bars.
        start = max(0, index - WINDOW_ROWS + 1)
        window = [bars[position] for position in range(start, index + 1)
                  if not ordered[position].get("is_suspended") and valid[position]]
        try:
            metrics = daily_metrics(window)
        except InsufficientBars:
            counts["insufficient_window"] += 1
            continue
        stage = classify_stage(metrics, None)["stage"]
        st = st_from_limit_band(symbol, _num(chain[0].get("pre_close")), _num(chain[0].get("limit_down")))
        if st is None:
            st = current_is_st
            counts["st_from_current_flag"] += 1
        loss = 1.0 - min(lows) / close_t  # type: ignore[type-var]
        samples.append((stage, board_key(symbol, st), loss, days[1] in holiday_resume_dates))
        counts["samples"] += 1
    return samples, counts


def holiday_resume_dates(market_sessions: Iterable[date], *, min_closed_days: int = HOLIDAY_CLOSURE_DAYS) -> frozenset[date]:
    """Market sessions that reopen after a closure of at least ``min_closed_days`` calendar days."""
    ordered = sorted(set(market_sessions))
    return frozenset(following for previous, following in zip(ordered, ordered[1:])
                     if (following - previous).days - 1 >= min_closed_days)


def _cell(losses: list[float], *, fallback: str | None, source: str, holiday: bool = False) -> dict[str, Any]:
    q99 = quantile(losses, PERCENTILE) * 100.0
    q95 = quantile(losses, SECONDARY_PERCENTILE) * 100.0
    cap = cap_from_q99(q99, minimum=HOLIDAY_CAP_MIN_PCT if holiday else CAP_MIN_PCT)
    return {"samples": len(losses), "q99_loss_pct": round(q99, 4), "q95_loss_pct": round(q95, 4),
            "cap_pct": cap, "fallback": fallback, "source_cell": source}


def build_cells(samples: dict[tuple[str, str], list[float]], stages: Iterable[str], boards: Iterable[str],
                *, holiday: bool = False) -> dict[str, dict[str, Any]]:
    """Cells keyed ``"<stage>|<board>"``; thin cells fall back to the board pooled across stages."""
    pooled: dict[str, list[float]] = {}
    for (stage, board), losses in samples.items():
        pooled.setdefault(board, []).extend(losses)
    everything = [loss for losses in samples.values() for loss in losses]
    cells: dict[str, dict[str, Any]] = {}
    for board in boards:
        board_losses = pooled.get(board, [])
        board_ok = len(board_losses) >= MIN_SAMPLES
        for stage in stages:
            own = samples.get((stage, board), [])
            key = f"{stage}|{board}"
            if len(own) >= MIN_SAMPLES:
                cells[key] = {**_cell(own, fallback=None, source=key, holiday=holiday)}
            elif board_ok:
                cells[key] = {**_cell(board_losses, fallback="board_pooled_across_stages",
                                      source=f"{POOLED}|{board}", holiday=holiday), "own_samples": len(own)}
            elif everything:
                cells[key] = {**_cell(everything, fallback="all_pooled", source=f"{POOLED}|{POOLED}", holiday=holiday),
                              "own_samples": len(own)}
        if board_losses:
            cells[f"{POOLED}|{board}"] = _cell(board_losses, fallback=None, source=f"{POOLED}|{board}", holiday=holiday)
    return cells


def artifact(*, stage_cells: dict[str, dict[str, Any]], holiday_cells: dict[str, dict[str, Any]],
             first_date: str, last_date: str, diagnostics: dict[str, Any]) -> dict[str, Any]:
    """The versioned artifact; ``version`` carries a digest of the cells so any change is a new version."""
    body = {"stage_cells": stage_cells, "holiday_cells": holiday_cells}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return {
        "version": f"{CALIBRATION_METHOD_VERSION}:{digest}",
        "method_version": CALIBRATION_METHOD_VERSION,
        "method": METHOD_TEXT,
        "tolerance_pct": TOLERANCE_PCT, "percentile": PERCENTILE, "secondary_percentile": SECONDARY_PERCENTILE,
        "horizon_sessions": HORIZON_SESSIONS, "max_gap_calendar_days": MAX_GAP_CALENDAR_DAYS,
        "min_samples": MIN_SAMPLES, "cap_rule": {"step_pct": CAP_STEP_PCT, "min_pct": CAP_MIN_PCT,
                                                  "max_pct": CAP_MAX_PCT, "holiday_min_pct": HOLIDAY_CAP_MIN_PCT},
        "holiday_closure_days": HOLIDAY_CLOSURE_DAYS,
        "price_basis": "adjusted research prices (close * adj_factor), missing factor -> sample dropped",
        "source_table": "quant.canonical_bars_daily",
        "data_window": {"first_date": first_date, "last_date": last_date},
        "boards": BOARD_LABEL,
        "stage_cells": stage_cells, "holiday_cells": holiday_cells,
        "diagnostics": diagnostics,
    }


# --------------------------------------------------------------------------
# generator-side lookup
# --------------------------------------------------------------------------
@lru_cache(maxsize=4)
def _load(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_calibration(path: Path | str | None = None) -> dict[str, Any]:
    return _load(str(path or ARTIFACT_PATH))


def lookup(calibration: dict[str, Any], stage: str, board: str, *, holiday: bool = False) -> dict[str, Any]:
    """The cell (with its basis) a plan's cap comes from; unknown stage/board use the pooled cells."""
    cells = calibration["holiday_cells" if holiday else "stage_cells"]
    for key in (f"{stage}|{board}", f"{POOLED}|{board}"):
        if key in cells:
            cell = cells[key]
            return {"stage": stage, "board": board, "board_label": BOARD_LABEL.get(board, board), "cell": key,
                    "cap_pct": cell["cap_pct"], "q99_loss_pct": cell["q99_loss_pct"],
                    "q95_loss_pct": cell["q95_loss_pct"], "samples": cell["samples"],
                    "fallback": cell.get("fallback") if key == f"{stage}|{board}" else "board_pooled_across_stages",
                    "source_cell": cell.get("source_cell", key), "tolerance_pct": calibration["tolerance_pct"],
                    "percentile": calibration["percentile"], "horizon_sessions": calibration["horizon_sessions"],
                    "calibration_version": calibration["version"],
                    "data_window": calibration["data_window"]}
    raise KeyError(f"no calibration cell for {stage}|{board}")


__all__ = [
    "ARTIFACT_PATH", "BOARDS", "BOARD_LABEL", "CALIBRATION_METHOD_VERSION", "CAP_MAX_PCT", "CAP_MIN_PCT",
    "HOLIDAY_CLOSURE_DAYS", "HORIZON_SESSIONS", "MAX_GAP_CALENDAR_DAYS", "METHOD_TEXT", "MIN_SAMPLES",
    "PERCENTILE", "POOLED", "TOLERANCE_PCT", "artifact", "board_key", "build_cells", "cap_from_q99",
    "generator_bar", "holiday_resume_dates", "load_calibration", "lookup", "quantile", "st_from_limit_band",
    "symbol_samples",
]
