import asyncio
from datetime import date

from app.user_tracking_refresh import refresh


def _workbench(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "name": "哈药股份",
        "as_of_date": "2026-09-04",
        "series": {"daily": [{"close": 7.78, "amount": 3_667_000_000, "turnover_rate": 17.83}]},
        "technical_summary": {"close": 7.78, "ma20": 8.67, "amount_multiple_5": 1.4},
        "flow": {"windows": {"5": {"net_amount": -509_300_000}}},
        "sectors": [{"label": "化学制药", "net_amount": -789_700_000}],
        "messages": [{"title": "半年度报告", "verification": "primary"}],
        "strategy_views": [{"key": "user_tracking", "next_session": [
            {"state": "向上确认", "condition": "站回20日均线"},
            {"state": "区间消化", "condition": "缩量横盘"},
            {"state": "向下失效", "condition": "跌破支撑"},
        ]}],
        "data_health": {key: {"status": "available"} for key in (
            "price", "volume", "vendor_flow", "sector", "messages", "scenario"
        )},
    }


def test_refresh_only_processes_active_user_tracking_and_persists_analysis() -> None:
    rows = [
        {"symbol": "600664.SH", "metadata": {"tracking_tags": [
            {"key": "user_requested_tracking", "source": "user", "active": True}
        ]}},
        {"symbol": "600000.SH", "metadata": {"tracking_tags": [
            {"key": "flow_sideways", "source": "strategy", "active": True}
        ]}},
    ]
    saved: dict[str, dict] = {}

    async def build(symbol: str, _day: date | None) -> dict:
        return _workbench(symbol)

    async def save(symbol: str, snapshot: dict) -> None:
        saved[symbol] = snapshot

    result = asyncio.run(refresh(
        rows, as_of_date=date(2026, 9, 4), build_workbench=build, save_snapshot=save,
    ))

    assert result["status"] == "completed"
    assert result["requested"] == 1
    assert set(saved) == {"600664.SH"}
    assert saved["600664.SH"]["headline"]
    assert saved["600664.SH"]["depends_on_holdings"] is False


def test_refresh_isolates_one_symbol_failure() -> None:
    rows = [{"symbol": symbol, "metadata": {"tracking_tags": [
        {"key": "user_requested_tracking", "source": "user", "active": True}
    ]}} for symbol in ("600664.SH", "600000.SH")]
    errors: dict[str, str] = {}
    saved: list[str] = []

    async def build(symbol: str, _day: date | None) -> dict:
        if symbol == "600000.SH":
            raise RuntimeError("provider unavailable")
        return _workbench(symbol)

    async def save(symbol: str, _snapshot: dict) -> None:
        saved.append(symbol)

    async def save_error(symbol: str, detail: str) -> None:
        errors[symbol] = detail

    result = asyncio.run(refresh(
        rows, as_of_date=None, build_workbench=build, save_snapshot=save, save_error=save_error,
    ))

    assert result["status"] == "partial"
    assert result["completed"] == 1
    assert result["failed"] == 1
    assert saved == ["600664.SH"]
    assert "provider unavailable" in errors["600000.SH"]
