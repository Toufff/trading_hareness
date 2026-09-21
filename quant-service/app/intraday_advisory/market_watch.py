"""Pure broad-index and industry-board monitoring for the advisory loop."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .rules import AdvisorySignal


SHANGHAI = ZoneInfo("Asia/Shanghai")
CORE_INDEX_SYMBOLS: dict[str, str] = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
}


@dataclass(frozen=True)
class IndexSample:
    symbol: str
    name: str
    observed_at: datetime
    price: float
    pre_close: float

    @property
    def daily_pct(self) -> float:
        return (self.price / self.pre_close - 1.0) * 100.0


@dataclass(frozen=True)
class SectorSample:
    sector_key: str
    label: str
    observed_at: datetime
    change_pct: float
    net_inflow: float | None


def _event_key(kind: str, direction: str, observed_at: datetime, labels: Sequence[str]) -> str:
    bucket = int(observed_at.timestamp()) // 600
    raw = f"{kind}|{direction}|{bucket}|{'|'.join(sorted(labels))}"
    return sha256(raw.encode()).hexdigest()


def index_sample_from_row(row: Mapping[str, Any], fetched_at: datetime) -> IndexSample | None:
    """Accept only a same-session Longhu index minute, never a stale close."""
    try:
        symbol = str(row["ts_code"])
        price, pre_close = float(row["price"]), float(row["pre_close"])
        provider_at = datetime.strptime(
            f"{row['trade_date']} {row['minute']}", "%Y%m%d %H:%M",
        ).replace(tzinfo=SHANGHAI)
    except (KeyError, TypeError, ValueError):
        return None
    age = (fetched_at.astimezone(SHANGHAI) - provider_at).total_seconds()
    # Longhu labels the currently forming index minute with its ending minute,
    # so a quote fetched late in 10:05 can legitimately carry ``10:06``.
    # Accept at most one minute of that vendor clock lead while retaining the
    # 90-second stale-data ceiling.
    if symbol not in CORE_INDEX_SYMBOLS or price <= 0 or pre_close <= 0 or not -60 <= age <= 90:
        return None
    return IndexSample(symbol, CORE_INDEX_SYMBOLS[symbol], fetched_at, price, pre_close)


def sector_samples_from_snapshot(snapshot: Mapping[str, Any] | None) -> tuple[SectorSample, ...]:
    if not snapshot or not isinstance(snapshot.get("items"), list):
        return ()
    observed_at = snapshot.get("snapshot_minute")
    if not isinstance(observed_at, datetime):
        return ()
    output: list[SectorSample] = []
    for row in snapshot["items"]:
        if not isinstance(row, Mapping) or row.get("taxonomy_key") != "eastmoney_industry":
            continue
        try:
            change = float(row["change_pct"])
            flow = float(row["net_inflow"]) if row.get("net_inflow") is not None else None
        except (KeyError, TypeError, ValueError):
            continue
        output.append(SectorSample(str(row.get("sector_key") or row.get("label")),
                                   str(row.get("label") or row.get("sector_key")),
                                   observed_at, change, flow))
    return tuple(output)


def _at_or_before(samples: Sequence[IndexSample], target: datetime) -> IndexSample | None:
    return next((item for item in reversed(samples) if item.observed_at <= target), None)


def evaluate_indices(series: Mapping[str, Sequence[IndexSample]]) -> AdvisorySignal | None:
    """Group correlated broad-index movement into one bounded alert card."""
    candidates: list[dict[str, Any]] = []
    for symbol, samples in series.items():
        if not samples:
            continue
        current = samples[-1]
        previous = samples[-2] if len(samples) >= 2 else None
        prior_60 = _at_or_before(samples, current.observed_at - timedelta(seconds=60))
        rapid = ((current.price / prior_60.price - 1.0) * 100.0) if prior_60 else 0.0
        crossed_daily = abs(current.daily_pct) >= 1.2 and (
            previous is None or abs(previous.daily_pct) < 1.2 or current.daily_pct * previous.daily_pct <= 0
        )
        if not crossed_daily and abs(rapid) < 0.35:
            continue
        candidates.append({"symbol": symbol, "name": current.name, "daily": current.daily_pct,
                           "rapid": rapid, "sample": current})
    if not candidates:
        return None
    candidates.sort(key=lambda item: max(abs(item["daily"]), abs(item["rapid"]) * 2), reverse=True)
    selected = candidates[:4]
    directions = {"up" if item["daily"] >= 0 else "down" for item in selected}
    direction = next(iter(directions)) if len(directions) == 1 else "mixed"
    observed_at = max(item["sample"].observed_at for item in selected)
    max_daily = max(abs(item["daily"]) for item in selected)
    max_rapid = max(abs(item["rapid"]) for item in selected)
    severity = "high" if max_daily >= 1.8 or max_rapid >= 0.6 else "medium"
    description = "；".join(
        f"{item['name']} 日内{item['daily']:+.2f}%/近1分钟{item['rapid']:+.2f}%" for item in selected
    )
    return AdvisorySignal(
        _event_key("broad_index_move", direction, observed_at, [item["symbol"] for item in selected]),
        "MARKET.INDEX", "沪深核心指数", "broad_index_move", direction, severity, observed_at,
        {"max_abs_daily_pct": round(max_daily, 3), "max_abs_60s_pct": round(max_rapid, 3),
         "affected_indices": float(len(selected))},
        f"核心指数出现值得复核的同步或快速波动：{description}",
    )


def evaluate_sectors(series: Mapping[str, Sequence[SectorSample]]) -> AdvisorySignal | None:
    """Group confirmed industry-board price moves; flow is supporting context."""
    candidates: list[dict[str, Any]] = []
    for key, samples in series.items():
        if len(samples) < 2:
            continue
        previous, current = samples[-2], samples[-1]
        if current.observed_at <= previous.observed_at:
            continue
        delta = current.change_pct - previous.change_pct
        crossed = abs(current.change_pct) >= 1.5 and abs(previous.change_pct) < 1.5
        accelerated = abs(delta) >= 0.6 and abs(current.change_pct) >= 0.8
        direction_confirmed = current.change_pct * delta > 0 or crossed
        if not direction_confirmed or not (crossed or accelerated):
            continue
        candidates.append({"key": key, "sample": current, "delta": delta})
    if not candidates:
        return None
    candidates.sort(key=lambda item: max(abs(item["sample"].change_pct), abs(item["delta"]) * 2), reverse=True)
    selected = candidates[:3]
    directions = {"up" if item["sample"].change_pct >= 0 else "down" for item in selected}
    direction = next(iter(directions)) if len(directions) == 1 else "mixed"
    observed_at = max(item["sample"].observed_at for item in selected)
    max_daily = max(abs(item["sample"].change_pct) for item in selected)
    max_acceleration = max(abs(item["delta"]) for item in selected)
    severity = "high" if max_daily >= 2.5 or max_acceleration >= 1.0 else "medium"
    description = "；".join(
        f"{item['sample'].label} {item['sample'].change_pct:+.2f}%（相邻快照{item['delta']:+.2f}pct，"
        f"净流{item['sample'].net_inflow:+.2f}亿）" if item["sample"].net_inflow is not None else
        f"{item['sample'].label} {item['sample'].change_pct:+.2f}%（相邻快照{item['delta']:+.2f}pct）"
        for item in selected
    )
    return AdvisorySignal(
        _event_key("industry_board_move", direction, observed_at, [item["key"] for item in selected]),
        "MARKET.SECTOR", "行业板块", "industry_board_move", direction, severity, observed_at,
        {"max_abs_daily_pct": round(max_daily, 3), "max_abs_snapshot_delta_pct": round(max_acceleration, 3),
         "affected_sectors": float(len(selected))},
        f"行业板块走势出现确认后的异动：{description}",
    )


def market_context(index_series: Mapping[str, Sequence[IndexSample]],
                   sector_series: Mapping[str, Sequence[SectorSample]]) -> dict[str, Any]:
    indices = [samples[-1] for samples in index_series.values() if samples]
    sectors = [samples[-1] for samples in sector_series.values() if samples]
    sectors.sort(key=lambda item: item.change_pct, reverse=True)
    return {
        "indices": [{"symbol": item.symbol, "name": item.name, "price": item.price,
                     "daily_pct": round(item.daily_pct, 3), "observed_at": item.observed_at.isoformat()}
                    for item in indices],
        "industry_boards": {
            "leaders": [{"label": item.label, "change_pct": item.change_pct, "net_inflow": item.net_inflow}
                        for item in sectors[:5]],
            "laggards": [{"label": item.label, "change_pct": item.change_pct, "net_inflow": item.net_inflow}
                         for item in sectors[-5:]],
        },
    }


__all__ = [
    "CORE_INDEX_SYMBOLS", "IndexSample", "SectorSample", "evaluate_indices", "evaluate_sectors",
    "index_sample_from_row", "market_context", "sector_samples_from_snapshot",
]
