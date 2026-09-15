from app.strategy_watchlist_projection import compact_post_close_watchlist


def test_restricted_discovery_is_visible_but_not_executable():
    payload = _payload()
    lanes = payload['latest_completed']['summary']['strategy_lanes']['lanes']
    lanes.append(dict(key='relay', label='情绪接力', selected=[], observation_list=[dict(
        symbol='000636.SZ', name='风华高科', state='regime_restricted',
        reason='板块同伴与涨停结构', caution='弱市，次日承接待观察',
        execution={'status':'wait_next_session', 'market_route':'disabled', 'buy_authorized':False})]))
    result = compact_post_close_watchlist(payload)
    row = next(r for r in result['items'] if r['symbol']=='000636.SZ')
    assert row['execution']['market_route'] == 'disabled'
    assert row['buy_authorized'] is False
    assert row['observation_state'] == 'regime_restricted'


def _payload():
    return {
        "latest_completed": {
            "as_of_date": "2026-09-04",
            "summary": {
                "strategy_lanes": {
                    "as_of_date": "2026-09-04",
                    "status": "completed",
                    "lanes": [
                        {"key": "accumulation", "label": "潜伏观察", "selected": [
                            {"symbol": "600001.SH", "name": "甲", "rank_score": 8, "reason": "资金横盘", "confirmation": "突破10", "invalidation": "跌破9"},
                        ]},
                        {"key": "trend", "label": "主线趋势", "selected": [
                            {"symbol": "600001.SH", "name": "甲", "rank_score": 10, "reason": "趋势"},
                            {"symbol": "600002.SH", "name": "乙", "rank_score": 12, "reason": "趋势"},
                        ]},
                    ],
                    "company_reviews": [
                        {"symbol": "600001.SH", "conclusion": "复核后保留", "risk": "拥挤", "business": "主营甲", "selection": {"disposition": "retain_watch"}},
                        {"symbol": "600002.SH", "conclusion": "不适合作为机会", "selection": {"disposition": "exclude"}},
                    ],
                    "review_coverage": {"planned": 2, "completed": 2, "missing_symbols": []},
                },
            },
        },
    }


def test_compact_watchlist_deduplicates_and_keeps_holdings_out():
    result = compact_post_close_watchlist(_payload())

    assert result["as_of_date"] == "2026-09-04"
    assert result["depends_on_holdings"] is False
    assert result["research_only"] is True
    assert [item["symbol"] for item in result["items"]] == ["600001.SH"]
    assert result["items"][0]["lane_labels"] == ["潜伏观察", "主线趋势"]
    assert result["items"][0]["review_status"] == "retain_watch"
    assert result["items"][0]["buy_authorized"] is False


def test_compact_watchlist_keeps_unreviewed_as_technical_observation():
    payload = _payload()
    payload["latest_completed"]["summary"]["strategy_lanes"]["company_reviews"] = []

    result = compact_post_close_watchlist(payload, limit=1)

    assert result["total_unique"] == 2
    assert len(result["items"]) == 1
    assert result["items"][0]["review_status"] == "technical_observation"


def test_compact_watchlist_keeps_user_requested_tracking_independent_from_strategies():
    result = compact_post_close_watchlist(
        {"latest_completed": {}},
        user_tracking=[{
            "symbol": "600664.SH",
            "label": "哈药股份",
            "metadata": {
                "tracking_tags": [{
                    "key": "user_requested_tracking",
                    "label": "用户主动跟踪",
                    "source": "user",
                    "active": True,
                }],
                "tracking_analysis": {
                    "reason": "用户要求持续跟踪短线量价、资金与事件变化。",
                    "confirmation": "放量站稳关键压力并获得板块共振。",
                    "invalidation": "跌破结构支撑且资金持续流出。",
                },
                "user_tracking_research": {
                    "status": "complete", "stance": "risk_repair",
                    "as_of_date": "2026-09-04", "headline": "结构仍弱，等待修复。",
                },
            },
        }],
    )

    assert result["total_unique"] == 1
    assert result["strategy_total_unique"] == 0
    assert result["user_tracking_total"] == 1
    item = result["items"][0]
    assert item["symbol"] == "600664.SH"
    assert item["name"] == "哈药股份"
    assert item["user_requested_tracking"] is True
    assert item["lane_labels"] == []
    assert item["tags"] == [{
        "key": "user_requested_tracking",
        "label": "用户主动跟踪",
        "source": "user",
    }]
    assert item["review_status"] == "user_tracking"
    assert item["reason"].startswith("用户要求")
    assert item["tracking_research"]["stance"] == "risk_repair"
    assert item["review_label"] == "人工跟踪研究已更新"


def test_compact_watchlist_merges_strategy_tags_without_overwriting_user_tag():
    payload = _payload()
    payload["latest_completed"]["summary"]["strategy_lanes"]["lanes"][0]["selected"].append({
        "symbol": "600664.SH",
        "name": "哈药股份",
        "rank_score": 9,
        "reason": "横盘后资金改善",
    })
    result = compact_post_close_watchlist(
        payload,
        user_tracking=[{
            "symbol": "600664.SH",
            "label": "哈药股份",
            "metadata": {
                "tracking_tags": [{
                    "key": "user_requested_tracking",
                    "label": "用户主动跟踪",
                    "source": "user",
                }],
            },
        }],
    )

    item = next(item for item in result["items"] if item["symbol"] == "600664.SH")
    assert item["user_requested_tracking"] is True
    assert item["lane_labels"] == ["潜伏观察"]
    assert item["tags"] == [
        {"key": "user_requested_tracking", "label": "用户主动跟踪", "source": "user"},
        {"key": "strategy:accumulation", "label": "潜伏观察", "source": "strategy"},
    ]


def test_compact_watchlist_keeps_manual_tracking_visible_after_company_exclusion():
    payload = _payload()
    result = compact_post_close_watchlist(
        payload,
        user_tracking=[{
            "symbol": "600002.SH",
            "label": "乙",
            "metadata": {
                "tracking_tags": [{
                    "key": "user_requested_tracking",
                    "label": "用户主动跟踪",
                    "source": "user",
                }],
            },
        }],
    )

    item = next(item for item in result["items"] if item["symbol"] == "600002.SH")
    assert item["review_status"] == "exclude"
    assert item["review_label"] == "公司复核未通过，仍按用户要求跟踪"
    assert item["buy_authorized"] is False
