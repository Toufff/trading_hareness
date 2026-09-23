from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.intraday_advisory.market_watch import (
    IndexSample, SectorSample, evaluate_indices, evaluate_sectors, index_sample_from_row,
    market_context, sector_samples_from_snapshot,
)


TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 22, 10, 0, tzinfo=TZ)


def test_index_parser_rejects_a_stale_previous_close() -> None:
    stale = {"ts_code": "000001.SH", "price": 3900, "pre_close": 3890,
             "trade_date": "20260921", "minute": "15:00"}
    assert index_sample_from_row(stale, NOW) is None
    current = {**stale, "trade_date": "20260922", "minute": "09:59"}
    assert index_sample_from_row(current, NOW) is not None


def test_grouped_index_alert_avoids_one_card_per_index() -> None:
    series = {}
    for symbol, name, start, end in (
        ("000001.SH", "上证指数", 3900, 3948),
        ("399006.SZ", "创业板指", 3300, 3350),
    ):
        series[symbol] = deque([
            IndexSample(symbol, name, NOW - timedelta(seconds=75), start, start),
            IndexSample(symbol, name, NOW, end, start),
        ])
    event = evaluate_indices(series)
    assert event is not None
    assert event.symbol == "MARKET.INDEX"
    assert event.metrics["affected_indices"] == 2
    assert "上证指数" in event.summary and "创业板指" in event.summary


def test_sector_alert_requires_confirmed_directional_acceleration() -> None:
    quiet = {"semiconductor": deque([
        SectorSample("semiconductor", "半导体", NOW - timedelta(minutes=1), 1.0, 2.0),
        SectorSample("semiconductor", "半导体", NOW, 1.2, 2.5),
    ])}
    assert evaluate_sectors(quiet) is None
    accelerated = {"semiconductor": deque([
        SectorSample("semiconductor", "半导体", NOW - timedelta(minutes=1), 0.9, 2.0),
        SectorSample("semiconductor", "半导体", NOW, 1.7, 4.5),
    ])}
    event = evaluate_sectors(accelerated)
    assert event is not None
    assert event.symbol == "MARKET.SECTOR"
    assert event.direction == "up"
    assert "净流" in event.summary


def test_longhu_sector_is_available_with_yuan_flow_normalized_and_watched_context() -> None:
    snapshot = {"snapshot_minute": NOW, "items": [{
        "taxonomy_key": "longhu_ths_industry", "sector_key": "881270",
        "label": "元件", "change_pct": 1.366, "net_inflow": -122_488_200,
    }]}
    (sample,) = sector_samples_from_snapshot(snapshot)
    assert sample.net_inflow == -1.224882
    context = market_context({}, {"longhu_ths_industry:881270": deque([sample])},
                             watched_sectors=("881270",))
    assert context["industry_boards"]["watched"][0]["label"] == "元件"
    assert context["industry_boards"]["watched"][0]["net_inflow_100m_cny"] == -1.224882
    assert context["industry_boards"]["watched"][0]["observed_at"] == NOW.isoformat()
