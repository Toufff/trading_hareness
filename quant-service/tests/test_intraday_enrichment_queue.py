from app.intraday_scan.enrichment import history_queue, minute_queue


def _lane(key, symbols):
    return {"key": key, "tracking_candidates": [
        {"symbol": symbol, "rank_score": 100 - index} for index, symbol in enumerate(symbols)
    ]}


def test_history_queue_keeps_live_lane_leaders_ahead_of_old_display_rows():
    scan = {"lanes": [_lane("trend", ["001232.SZ", "600001.SH"]),
                      _lane("expansion", ["600002.SH"])]}
    old = [{"symbol": f"600{i:03d}.SH", "display_rank": 1} for i in range(130)]
    queue, coverage = history_queue(scan, old, ["001232.SZ", "600003.SH"], limit=96)
    assert queue[0] == "001232.SZ"
    assert "600002.SH" in queue
    assert len(queue) == 96
    assert coverage["priority_missing"] == []
    assert coverage["old_display_deferred"] > 0


def test_minute_queue_keeps_all_current_front_rows_even_with_130_old_rows():
    lanes = [
        {"key": "trend", "items": [
            {"symbol": "001232.SZ", "formal_rank": 30, "source": "new_intraday"},
            {"symbol": "600001.SH", "formal_rank": 20, "source": "new_intraday"},
        ]},
        {"key": "expansion", "items": [
            {"symbol": "600002.SH", "formal_rank": 25, "source": "new_intraday"},
        ]},
    ]
    old = [{"symbol": f"600{i:03d}.SH", "display_rank": 1} for i in range(130)]
    queue, coverage = minute_queue(lanes, old, limit=96)
    assert queue[:3] == ["001232.SZ", "600002.SH", "600001.SH"]
    assert len(queue) == 96
    assert coverage["priority_missing"] == []
    assert coverage["old_display_deferred"] > 0


def test_budget_shortfall_is_explicit_not_a_successful_coverage_receipt():
    scan = {"lanes": [_lane("trend", ["001232.SZ", "600001.SH"])]}
    queue, coverage = history_queue(scan, [], [], limit=1)
    assert queue == ["001232.SZ"]
    assert coverage["priority_missing"] == ["600001.SH"]
