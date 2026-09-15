from datetime import datetime, timezone
from decimal import Decimal

from app.user_tracking_research import build_snapshot, has_active_user_tracking_tag


def _workbench() -> dict:
    return {
        "contract_version": "stock-workbench-v2",
        "symbol": "600664.SH",
        "name": "哈药股份",
        "industry": "化学制药",
        "as_of_date": "2026-09-04",
        "generated_at": "2026-09-05T23:00:00+08:00",
        "series": {"daily": [{
            "date": "2026-09-04", "close": 7.78, "change_pct": -9.01,
            "amount": 3_667_428_872, "turnover_rate": 17.83,
            "pe": 38.26, "pb": 3.21,
        }]},
        "technical_summary": {
            "close": 7.78, "ma5": 8.84, "ma20": 8.67, "rsi14": 48.6,
            "atr14": 0.60, "amount_multiple_5": 1.15,
            "support_5": 7.71, "resistance_10": 9.51,
            "return_5d_pct": -13.56, "return_20d_pct": 13.74,
        },
        "flow": {"status": "ready", "windows": {
            "1": {"net_amount": -130_418_835, "positive_days": 0, "observations": 1},
            "5": {"net_amount": -509_257_206, "positive_days": 1, "observations": 5},
            "10": {"net_amount": -177_169_593, "positive_days": 5, "observations": 10},
        }},
        "sectors": [{"label": "化学制药", "change_pct": -0.584, "net_amount": -789_657_706}],
        "market_context": {"status": "ready", "regime": {"regime_label": "mixed_rotation"}},
        "messages": [{
            "id": "a", "title": "2026年半年度报告", "category": "company",
            "verification": "一手披露", "occurred_at": "2026-08-26T00:00:00+08:00",
            "available_at": "2026-08-26T00:00:00+08:00", "url": "https://example.test/a.pdf",
        }],
        "strategy_views": [{
            "key": "user_tracking", "status": "ready",
            "current_reading": ["结构走弱，资金和板块尚未修复"],
            "next_session": [
                {"state": "向上确认", "condition": "站回8.67且资金改善", "action": "继续观察"},
                {"state": "区间消化", "condition": "守住7.71", "action": "等待"},
                {"state": "向下失效", "condition": "跌破7.57不能收复", "action": "取消观察"},
            ],
            "next_week": [],
        }],
        "data_health": {
            "price": {"status": "ready", "detail": "120日日线"},
            "volume": {"status": "ready", "detail": "成交额"},
            "vendor_flow": {"status": "ready", "detail": "资金"},
            "sector": {"status": "ready", "detail": "板块"},
            "messages": {"status": "ready", "detail": "公告"},
            "scenario": {"status": "ready", "detail": "情景"},
        },
        "artifact_freshness": {
            "price": {"status": "ready", "as_of_date": "2026-09-04"},
            "flow": {"status": "ready", "as_of_date": "2026-09-04"},
            "messages": {"status": "current", "latest_available_at": "2026-08-26T00:00:00+08:00"},
        },
    }


def test_user_tracking_snapshot_contains_human_analysis_not_only_a_tag():
    result = build_snapshot(_workbench())

    assert result["status"] == "complete"
    assert result["stance"] == "risk_repair"
    assert result["as_of_date"] == "2026-09-04"
    assert result["price_structure"]["close"] == 7.78
    assert result["capital_flow"]["windows"]["5"]["net_amount"] == -509_257_206
    assert result["sector"]["label"] == "化学制药"
    assert result["valuation"] == {"pe": 38.26, "pb": 3.21}
    assert result["events"][0]["title"] == "2026年半年度报告"
    assert result["conditions"]["confirmation"] == "站回8.67且资金改善"
    assert result["conditions"]["invalidation"] == "跌破7.57不能收复"
    assert result["buy_authorized"] is False


def test_active_user_tracking_tag_is_explicit_and_not_inferred_from_watchlist_presence():
    assert has_active_user_tracking_tag({"tracking_tags": [{
        "key": "user_requested_tracking", "source": "user", "active": True,
    }]}) is True
    assert has_active_user_tracking_tag({"tracking_tags": [{
        "key": "user_requested_tracking", "source": "user", "active": False,
    }]}) is False
    assert has_active_user_tracking_tag({"tracking_reason": "strategy_refresh"}) is False


def test_snapshot_is_json_serializable_with_database_temporal_and_decimal_values():
    workbench = _workbench()
    workbench["generated_at"] = datetime(2026, 9, 5, 15, 0, tzinfo=timezone.utc)
    workbench["market_context"] = {"observed_at": datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc)}
    workbench["artifact_freshness"] = {"age_hours": Decimal("3.5")}

    snapshot = build_snapshot(workbench)

    assert snapshot["generated_at"] == "2026-09-05T15:00:00+00:00"
    assert snapshot["market"]["observed_at"] == "2026-09-04T07:00:00+00:00"
    assert snapshot["artifact_freshness"]["age_hours"] == 3.5
