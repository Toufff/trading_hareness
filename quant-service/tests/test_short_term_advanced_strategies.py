from datetime import date, timedelta

from app.short_term_lanes.advanced_strategies import evaluate, ohlc_features
from app.short_term_lanes.regime import classify, route
from app.short_term_lanes.risk import risk_envelope


def bars(*, reclaim=False):
    start = date(2026, 6, 1)
    rows = []
    close = 10.0
    for index in range(45):
        if index < 35:
            close = 10 + (0.22 if index % 2 else -0.18)
            spread = 0.24
            amount = 600_000_000
        else:
            close = 10.08 + (index - 35) * 0.006
            spread = 0.045
            amount = 360_000_000
        rows.append({
            "date": str(start + timedelta(days=index)), "open": close - 0.01,
            "high": close + spread, "low": close - spread, "close": close, "amount": amount,
        })
    if reclaim:
        rows[-2].update(open=10.2, high=10.25, low=9.45, close=9.5, amount=650_000_000)
        rows[-1].update(open=9.55, high=10.15, low=9.5, close=10.12, amount=700_000_000)
    else:
        prior_high = max(row["high"] for row in rows[-11:-1])
        rows[-1].update(open=prior_high - 0.01, high=prior_high + 0.20,
                        low=prior_high - 0.03, close=prior_high + 0.10, amount=650_000_000)
    return rows


def test_contraction_requires_strict_ohlc_and_confirms_first_expansion():
    base = {"change_pct": 2.0, "amount_multiple": 1.3}
    sector = {"latest_breadth": 0.62}
    assert ohlc_features(bars()[:20], "2026-12-31") is None
    assert ohlc_features(bars(), "2026-12-31") is None  # stale cannot pass
    metrics = ohlc_features(bars(), bars()[-1]['date'])
    assert metrics is not None
    matched, _score, reason, details = evaluate(
        "contraction", base, ohlc=metrics, sector_rotation=sector,
        events=[], verified_negative=False,
    )
    assert matched
    assert "真实波幅" in reason
    assert details["history_rows"] >= 40


def test_reclaim_is_blocked_by_verified_negative_event():
    metrics = ohlc_features(bars(reclaim=True), bars(reclaim=True)[-1]['date'])
    base = {"change_pct": 3.0, "amount_multiple": 1.1}
    sector = {"latest_breadth": 0.55}
    matched, *_ = evaluate("reclaim", base, ohlc=metrics, sector_rotation=sector, events=[], verified_negative=False)
    assert matched
    blocked, *_ = evaluate("reclaim", base, ohlc=metrics, sector_rotation=sector,
                           events=[{"impact_direction": "negative"}], verified_negative=True)
    assert not blocked


def test_rotation_needs_multi_day_breadth_flow_and_leader_confirmation():
    base = {"change_pct": 2.2, "amount_multiple": 1.1}
    sector = {
        "recent_breadth": 0.66, "breadth_acceleration": 0.18,
        "median_change_3d": 0.7, "flow_3d": 100_000_000,
        "stable_leaders": 2, "latest_breadth": 0.7,
    }
    matched, *_ = evaluate("rotation", base, ohlc=None, sector_rotation=sector,
                           events=[], verified_negative=False)
    assert matched
    sector["flow_3d"] = -1
    matched, *_ = evaluate("rotation", base, ohlc=None, sector_rotation=sector,
                           events=[], verified_negative=False)
    assert not matched


def test_regime_routes_strategy_without_fake_probability_and_risk_is_separate():
    features = {str(index): {"change_pct": 1, "return_10d": 4} for index in range(10)}
    sectors = {"a": {"members": 10, "up_fraction": 0.8, "return10_median": 6}}
    regime = classify(features, sectors)
    assert regime["label"] == "broad_risk_on"
    assert regime["probability_status"] == "not_calibrated"
    assert route("trend", regime)["state"] == "preferred"
    risk = risk_envelope("trend", {"close": 10, "recent_low": 9.5, "volatility": 2}, regime)
    assert risk["max_single_position_pct"] == 8
    assert risk["research_only"]
    assert any("T+1" in item for item in risk["execution_constraints"])
