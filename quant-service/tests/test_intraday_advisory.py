from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app.intraday_advisory.rules import AdvisorySignal, QuoteSample, evaluate
from app.intraday_advisory.renderer import analysis_card, signal_card
from app.intraday_advisory.presentation import ensure_readable_card, humanize_card, humanize_text, metric_lines
from app.intraday_advisory.market_watch import index_sample_from_row
from app.intraday_advisory.schedule import decide
from app.intraday_advisory.scope import AdvisoryScope, ScopeItem
from app.intraday_advisory.repository import recent_quote_rows
from app.intraday_advisory.model import CodexAdvisoryModel, _normalized_response
from app.agent_paper.model import ModelFailure
from app.intraday_advisory.runtime import (
    IntradayAdvisoryDependencies, RuntimeState, _analyze, _context, _deepseek_push_worthy, _drain,
    _hydrate_quotes,
    run_intraday_advisory_cycle,
)
from app.async_intraday_advisory_read_repository import humanize_event


TZ = ZoneInfo("Asia/Shanghai")
MONDAY = datetime(2026, 9, 21, 10, 0, tzinfo=TZ)


def test_index_sample_accepts_current_forming_minute_label() -> None:
    fetched_at = datetime(2026, 9, 21, 10, 5, 49, tzinfo=TZ)
    row = {
        "ts_code": "000001.SH", "price": 3931.41, "pre_close": 3911.87,
        "trade_date": "20260921", "minute": "10:06",
    }
    assert index_sample_from_row(row, fetched_at) is not None


def test_model_context_normalizes_database_decimal_values() -> None:
    scope = AdvisoryScope("test", (
        ScopeItem("603650.SH", "彤程新材", "holding", {
            "quantity": Decimal("100"), "market_price": Decimal("72.90"),
        }),
    ), "snapshot", "decision", ())
    state = RuntimeState()
    state.pending_events.append({"trigger_price": Decimal("72.50"),
                                 "summary": "主动侧代理净流出只能作为代理信号"})

    payload = _context(scope, state, MONDAY, trigger_kind="scheduled", report_kind="ten_minute")

    json.dumps(payload, ensure_ascii=False)
    assert payload["scope"][0]["position_or_recommendation"]["quantity"] == "100"
    assert 'market_price' not in payload['scope'][0]['position_or_recommendation']  # stale snapshot price is not live
    assert payload["recent_events"][0]["trigger_price"] == "72.50"
    assert payload["recent_events"][0]["summary"] == "内盘增量占优只能作为内外盘方向信号"


def test_context_preserves_verified_snapshot_time_and_exact_watched_sector() -> None:
    scope = AdvisoryScope("test", (ScopeItem("603936.SH", "博敏电子", "recommendation", {
        "ranking_reference": {"sector_key": "881270", "sector_label": "元件"},
    }),), "snapshot", "decision", (), MONDAY - timedelta(hours=1))
    state = RuntimeState()
    from app.intraday_advisory.market_watch import SectorSample
    state.sector_samples["longhu_ths_industry:881270"].append(
        SectorSample("881270", "元件", MONDAY, 1.3, -1.2, "longhu_ths_industry"))
    payload = _context(scope, state, MONDAY, trigger_kind="scheduled", report_kind="fixed")
    assert payload["broker_evidence"]["holding_snapshot_observed_at"] == (
        MONDAY - timedelta(hours=1)).isoformat()
    assert payload["market_context"]["industry_boards"]["watched"][0]["label"] == "元件"


def test_restart_hydrates_valid_same_session_history_without_emitting_signals() -> None:
    state = RuntimeState()
    rows = []
    for offset in range(0, 181, 5):
        at = MONDAY - timedelta(seconds=180-offset)
        rows.append({"symbol": "600000.SH", "observed_at": at,
                     "raw": {"ts_code": "600000.SH", "name": "浦发银行", "price": 10.0,
                             "pre_close": 10.0, "cumulative_amount": 100_000 + offset * 1000,
                             "cumulative_volume_lot": 1000 + offset * 10,
                             "trade_time": at.strftime("%Y%m%d%H%M%S")}})
    rows.insert(0, {"symbol": "600000.SH", "observed_at": MONDAY - timedelta(days=1),
                    "raw": rows[0]["raw"]})
    _hydrate_quotes(state, rows, MONDAY)
    assert len(state.samples["600000.SH"]) == 37
    assert state.samples["600000.SH"][-1].observed_at == MONDAY
    assert state.pending_events == []
    assert len(_context(AdvisoryScope("test", (ScopeItem("600000.SH", "浦发银行", "holding", {}),),
                                      None, None, ()), state, MONDAY,
                        trigger_kind="scheduled", report_kind="fixed")["scope"][0]["windows"]) > 0


def test_restart_history_query_is_bounded_to_session_and_source() -> None:
    class Connection:
        def execute(self, sql, params):
            self.sql, self.params = sql, params
            return self

        def fetchall(self):
            return [{"symbol": "600000.SH", "observed_at": MONDAY, "raw": {}}]

    connection = Connection()
    rows = recent_quote_rows(connection, symbols=["600000.SH"], at=MONDAY)
    assert rows[0]["symbol"] == "600000.SH"
    assert "source_name='longhu_order_book'" in connection.sql
    assert 'ORDER BY observed_at DESC LIMIT %s' in connection.sql
    assert connection.params[1] == MONDAY - timedelta(minutes=35)  # 09:25 session floor
    assert connection.params[3] == 600


def test_codex_advisory_rediscovers_desktop_binary_after_upgrade() -> None:
    with patch.dict(os.environ, {"INTRADAY_ADVISORY_CODEX_BIN": ""}), \
         patch("app.intraday_advisory.model.find_codex_executable", side_effect=["old.exe", "new.exe"]), \
         patch("app.intraday_advisory.model.subprocess.run", side_effect=[
             FileNotFoundError(), subprocess.CompletedProcess([], 0, stdout="ok", stderr="")]) as run, \
         patch("app.intraday_advisory.model.parse_codex_stream", return_value=({}, {}, [])), \
         patch("app.intraday_advisory.model._normalized_response", return_value={"headline": "ok"}):
        model = CodexAdvisoryModel()
        result = model._run({})
    assert result.output["headline"] == "ok"
    assert run.call_count == 2
    assert run.call_args.args[0][0] == "new.exe"


def test_codex_advisory_respects_explicit_binary_pin() -> None:
    with patch.dict(os.environ, {"INTRADAY_ADVISORY_CODEX_BIN": "missing.exe"}), \
         patch("app.intraday_advisory.model.find_codex_executable") as find, \
         patch("app.intraday_advisory.model.subprocess.run", side_effect=FileNotFoundError()):
        model = CodexAdvisoryModel()
        try:
            model._run({})
            assert False, "expected cli_unavailable"
        except ModelFailure as error:
            assert error.code == "cli_unavailable"
    find.assert_not_called()


def test_full_brief_downgrades_generic_missing_claims_to_as_of_boundary() -> None:
    payload = {"as_of": MONDAY.isoformat(),
               "broker_evidence": {"holding_snapshot_observed_at": (MONDAY-timedelta(hours=1)).isoformat()},
               "market_context": {"industry_boards": {"watched": [{
                   "label": "元件", "observed_at": (MONDAY-timedelta(minutes=1)).isoformat()}]}}}
    output = _normalized_response({"market_state": "watch", "risks": [
        "元件板块缺少本时点数据", "缺少成交价、费用及经核实的可卖数量", "跌破止损线"],
        "holding_focus": [], "recommendation_focus": []}, payload)
    assert output['risks'] == ["跌破止损线"]
    assert any('09:00' in value and '已核实' in value for value in output['data_boundaries'])
    card = analysis_card('codex', output, report_kind='tail', generated_at=MONDAY)
    assert '阅读边界' in json.dumps(card, ensure_ascii=False)
    assert '缺少成交价' not in json.dumps(card, ensure_ascii=False)


def test_brief_names_real_quote_gap_instead_of_generic_missing_information() -> None:
    payload = {"as_of": MONDAY.isoformat(), "scope": [{"symbol": "002315.SZ", "name": "焦点科技",
        "windows": {"180": {"status": "insufficient_window", "reason": "sample_gap",
            "availability_note": "09:56:36–09:58:24 采样中断 108 秒，不能计算连续窗口。"}}}]}
    output = _normalized_response({"market_state": "watch", "risks": [
        "近三分钟和五分钟信号不足，信息缺失", "跌破止损线"],
        "holding_focus": [], "recommendation_focus": []}, payload)
    assert output['risks'] == ["跌破止损线"]
    assert output['data_boundaries'] == [
        "焦点科技：09:56:36–09:58:24 采样中断 108 秒，不能计算连续窗口。"]


def test_schedule_uses_bounded_cadences_and_special_reports() -> None:
    regular = decide(MONDAY, last_fetch=MONDAY - timedelta(seconds=5),
                     last_deepseek=MONDAY - timedelta(minutes=10),
                     last_codex=MONDAY - timedelta(minutes=30))
    assert regular.fetch_quotes and not regular.run_deepseek and regular.run_codex
    assert regular.report_kind == "fixed"
    suppressed = decide(MONDAY.replace(hour=11, minute=30), last_fetch=None, last_deepseek=MONDAY,
                        last_codex=MONDAY.replace(hour=11, minute=0))
    assert not suppressed.run_codex
    midday = decide(MONDAY.replace(hour=11, minute=35), last_fetch=MONDAY, last_deepseek=MONDAY,
                    last_codex=MONDAY.replace(hour=11, minute=0))
    assert midday.run_codex and midday.report_kind == "midday"
    tail = decide(MONDAY.replace(hour=14, minute=45), last_fetch=MONDAY, last_deepseek=MONDAY,
                  last_codex=MONDAY.replace(hour=14, minute=0))
    assert tail.run_codex and tail.report_kind == "tail"


def test_schedule_checks_conditions_between_three_briefings() -> None:
    ten = decide(MONDAY.replace(minute=10), last_fetch=MONDAY,
                 last_deepseek=MONDAY, last_codex=MONDAY)
    assert ten.run_deepseek and not ten.run_codex
    twenty = decide(MONDAY.replace(minute=20), last_fetch=MONDAY,
                    last_deepseek=MONDAY.replace(minute=10), last_codex=MONDAY)
    assert twenty.run_deepseek and not twenty.run_codex
    thirty = decide(MONDAY.replace(minute=30), last_fetch=MONDAY,
                    last_deepseek=MONDAY.replace(minute=20), last_codex=MONDAY)
    assert thirty.run_deepseek and not thirty.run_codex
    arbitrary_restart = decide(MONDAY.replace(minute=21), last_fetch=MONDAY,
                               last_deepseek=None, last_codex=MONDAY)
    assert not arbitrary_restart.run_deepseek and not arbitrary_restart.run_codex


def test_cards_translate_internal_fields_and_bound_model_output() -> None:
    signal = AdvisorySignal(
        event_key="test", symbol="002008.SZ", name="大族激光", kind="amount_pulse",
        direction="inflow", severity="medium", observed_at=MONDAY,
        metrics={"amount_ratio": 4.2, "active_ratio": 0.3},
        summary="1分钟成交额放大至基线 4.2 倍，外盘增量占优",
    )
    first = signal_card(signal, source="recommendation", dashboard_url="https://stock.toufai.top")
    ensure_readable_card(first)
    serialized = json.dumps(first, ensure_ascii=False)
    assert first["schema"] == "2.0"
    assert first["config"]["width_mode"] == "fill"
    assert any(element["tag"] == "collapsible_panel" for element in first["body"]["elements"])
    panels = [element for element in first["body"]["elements"] if element["tag"] == "collapsible_panel"]
    assert all(panel["header"]["padding"].count("px") == 4 for panel in panels)
    assert any(element["tag"] == "button" for element in first["body"]["elements"])
    assert "elements" not in first
    assert "amount_ratio" not in serialized and "002008.SZ" not in serialized
    assert "外盘增量占优" in serialized and "内外盘差约占成交量" in serialized
    assert "主动侧" not in serialized and "偏流入" not in serialized and "净流入" not in serialized
    report = analysis_card("codex", {
        "market_state": "watch", "headline": "科技方向分化",
        "market_summary": "核心指数震荡，通信板块承接仍需确认",
        "holding_focus": [{"symbol": "002008.SZ", "name": "大族激光", "status": "breakout_hold 进入复核",
                           "evidence": "最新价 31.20 元", "action": "核对纪律线"}],
        "recommendation_focus": [{"symbol": "600664.SH", "name": "哈药股份", "status": "等待确认",
                                  "evidence": "量能没有继续放大", "action": "buy_authorized=false"}],
        "risks": ["quote=null"],
    }, report_kind="ten_minute", generated_at=MONDAY)
    ensure_readable_card(report)
    rendered = json.dumps(report, ensure_ascii=False)
    assert report["schema"] == "2.0"
    assert len(report["header"]["text_tag_list"]) == 3
    assert any(element["tag"] == "column_set" for element in report["body"]["elements"])
    assert any(element["tag"] == "collapsible_panel" for element in report["body"]["elements"])
    assert "需要关注" in rendered and "当前尚未满足买入条件" in rendered
    assert "quote=null" not in rendered and "breakout_hold" not in rendered
    assert rendered.index("大盘") < rendered.index("持仓关注") < rendered.index("推荐池关注")
    assert "Codex 复核" in rendered

    private = signal_card(
        signal, source="recommendation",
        dashboard_url="https://stock.toufai.top/_access/example",
    )
    private_rendered = json.dumps(private, ensure_ascii=False)
    assert "_access/example/market-decision" not in private_rendered
    assert "https://stock.toufai.top/_access/example" in private_rendered


def test_analysis_card_promotes_legacy_morning_guidance_into_readable_focus_panels() -> None:
    report = analysis_card("codex", {
        "market_state": "watch",
        "summary": "主要指数上涨，但个股分化明显。",
        "guidance": [
            "持仓哈药股份现价8.25元、涨10.00%，成交额15.17亿元；重点观察午后封板承接。",
            "推荐标的大族激光现价97.23元、跌2.38%，成交额33.45亿元；尚未满足确认条件，不追价。",
        ],
        "attention_symbols": ["600664.SH", "002008.SZ"],
        "risks": ["可卖数量需要按账户快照核对。"],
    }, report_kind="midday", generated_at=MONDAY)

    rendered = json.dumps(report, ensure_ascii=False)
    assert "哈药股份" in rendered and "大族激光" in rendered
    assert "盘中涨停或接近涨停" in rendered and "盘中回撤" in rendered
    assert "持仓 1" in rendered and "候选 1" in rendered
    assert "600664.SH" not in rendered and "002008.SZ" not in rendered
    assert any(element["tag"] == "collapsible_panel" for element in report["body"]["elements"])


def test_deepseek_is_persisted_but_only_material_new_changes_are_push_worthy() -> None:
    routine = {"should_notify": False, "state_fingerprint": "routine", "market_state": "watch",
               "notification_reason": "", "holding_focus": [], "recommendation_focus": [], "risks": []}
    assert not _deepseek_push_worthy(routine, None)
    material = {"should_notify": True, "state_fingerprint": "new", "market_state": "watch",
                "notification_reason": "持仓跌破盘中关键承接位",
                "holding_focus": [{"symbol": "600000.SH"}], "recommendation_focus": [], "risks": []}
    assert _deepseek_push_worthy(material, "old")
    assert not _deepseek_push_worthy(material, "new")
    empty_claim = {**material, "holding_focus": [], "notification_reason": ""}
    assert not _deepseek_push_worthy(empty_claim, "old")


def test_incomplete_market_context_is_not_sent_to_a_model_or_user() -> None:
    async def scenario() -> None:
        model = AsyncMock()
        deps = IntradayAdvisoryDependencies(
            database=object(), run_database=AsyncMock(), fetch_quotes=AsyncMock(),
            fetch_indices=AsyncMock(), post_text=AsyncMock(), post_card=AsyncMock(),
            session_open=AsyncMock(), now=lambda: MONDAY, account_key=lambda: "citics-primary",
            deepseek_factory=lambda: model,
        )
        scope = AdvisoryScope("citics-primary", (
            ScopeItem("002008.SZ", "大族激光", "recommendation", {}),
        ), "snapshot", "decision", ())
        result = await _analyze(deps, RuntimeState(), scope, provider="deepseek",
                                trigger_kind="scheduled", report_kind="ten_minute", always_push=False)
        assert result["status"] == "skipped"
        assert result["reason"] == "insufficient_fresh_market_context"
        model.analyze.assert_not_awaited()
        deps.post_card.assert_not_awaited()

    asyncio.run(scenario())


def test_rules_detect_amount_pulse_without_calling_it_institutional_money() -> None:
    samples: list[QuoteSample] = []
    for index in range(145):
        at = MONDAY - timedelta(seconds=(144 - index) * 5)
        minute = index // 12
        # Baseline minute ~= 1m; final minute jumps by ~= 7m.
        amount = index * 83_333 if index < 133 else 133 * 83_333 + (index - 132) * 583_333
        volume = index * 100 if index < 133 else 133 * 100 + (index - 132) * 700
        outer = volume * (0.52 if index < 133 else 0.80)
        inner = volume - outer
        samples.append(QuoteSample("600000.SH", at, 10 + minute * 0.001, 10, amount,
                                   volume, outer, inner, "浦发银行"))
    events = evaluate(samples)
    pulse = next(event for event in events if event.kind == "amount_pulse")
    assert pulse.direction == "inflow"
    assert pulse.metrics["amount_ratio"] >= 3
    assert "机构" not in pulse.summary and "主力" not in pulse.summary
    assert "外盘增量占优" in pulse.summary
    assert "主动侧" not in pulse.summary and "净流入" not in pulse.summary


def test_negative_inner_outer_volume_evidence_uses_common_market_language() -> None:
    rows = metric_lines({"active_ratio": -0.35})

    assert rows == ["近1分钟内盘增量占优，内外盘差约占成交量 35.0%"]


def test_legacy_flow_proxy_language_is_normalized_at_presentation_boundary() -> None:
    original = {"elements": [{"text": {"content":
        "浪潮信息出现主动侧代理净流出；彤程新材出现主动侧成交代理偏流入"}}]}
    normalized = humanize_card(original)
    rendered = json.dumps(normalized, ensure_ascii=False)

    assert "浪潮信息出现内盘增量占优" in rendered
    assert "彤程新材出现外盘增量占优" in rendered
    assert "主动侧" not in rendered and "代理净流" not in rendered
    ensure_readable_card(normalized)
    assert humanize_text("短周期代理信号") == "短周期内外盘信号"


def test_status_read_model_does_not_expose_persisted_legacy_flow_terms() -> None:
    event = humanize_event({"symbol": "000977.SZ",
                            "summary": "1分钟放量且主动侧代理净流出"})

    assert event["summary"] == "1分钟放量且内盘增量占优"


def test_delivery_drain_normalizes_cards_queued_before_deploy() -> None:
    async def scenario() -> None:
        calls = 0

        async def run_database(call):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [{"delivery_id": "old", "message_text": "",
                         "message_card": {"elements": [{"text": {"content":
                             "1分钟放量且主动侧代理净流出"}}]}}]
            return None

        deps = IntradayAdvisoryDependencies(
            database=object(), run_database=run_database, fetch_quotes=AsyncMock(),
            fetch_indices=AsyncMock(), post_text=AsyncMock(),
            post_card=AsyncMock(return_value={"status": "sent"}),
            session_open=AsyncMock(), now=lambda: MONDAY,
            account_key=lambda: "citics-primary",
        )
        outcome = await _drain(deps)

        assert outcome["sent"] == 1
        sent = json.dumps(deps.post_card.await_args.args[0], ensure_ascii=False)
        assert "内盘增量占优" in sent
        assert "主动侧" not in sent

    asyncio.run(scenario())


def test_market_signal_metrics_are_human_readable() -> None:
    rows = metric_lines({"max_abs_daily_pct": 1.41, "max_abs_60s_pct": 0.36,
                         "affected_indices": 4, "max_abs_snapshot_delta_pct": 0.82,
                         "affected_sectors": 3})
    assert rows == [
        "核心指数最大日内幅度 1.41%", "核心指数最大近1分钟幅度 0.36%", "涉及 4 个核心指数",
        "行业板块相邻快照最大变化 0.82 个百分点", "涉及 3 个行业板块",
    ]


def test_deterministic_delivery_does_not_spawn_a_second_model_card() -> None:
    asyncio.run(_deterministic_delivery_precedes_bundled_codex_analysis())


async def _deterministic_delivery_precedes_bundled_codex_analysis() -> None:
    scope = AdvisoryScope("citics-primary", (ScopeItem("600000.SH", "浦发银行", "holding", {}),),
                          "snapshot", "decision", ())
    state = RuntimeState(pressure_day=MONDAY.date())
    state.last_deepseek = MONDAY
    state.last_codex = MONDAY
    for index in range(25):
        at = MONDAY - timedelta(seconds=(25 - index) * 5)
        state.samples["600000.SH"].append(QuoteSample(
            "600000.SH", at, 10, 10, index * 100_000, index * 100, index * 55, index * 45, "浦发银行"))
    calls: list[str] = []

    async def run_database(call):
        return call()

    async def fetch(_symbols, **_kwargs):
        return [{"ts_code": "600000.SH", "name": "浦发银行", "price": 10.2, "pre_close": 10,
                 "cumulative_amount": 12_000_000, "cumulative_volume_lot": 12_000,
                 "outer_volume_lot": 9_000, "inner_volume_lot": 3_000,
                 "trade_time": MONDAY.strftime("%Y%m%d%H%M%S")}]

    deps = IntradayAdvisoryDependencies(
        database=object(), run_database=run_database, fetch_quotes=fetch,
        fetch_indices=AsyncMock(return_value={}),
        post_text=AsyncMock(return_value={"status": "sent"}),
        post_card=AsyncMock(return_value={"status": "sent"}),
        session_open=AsyncMock(return_value=(True, "open")), now=lambda: MONDAY,
        account_key=lambda: "citics-primary",
    )
    with patch("app.intraday_advisory.runtime._scope", return_value=scope), \
         patch("app.intraday_advisory.runtime._persist_rows", return_value=1), \
         patch("app.intraday_advisory.runtime._recent_quotes", return_value=[]), \
         patch("app.intraday_advisory.runtime._persist_event_and_delivery",
               return_value={"event_id": "event", "event_key": "key"}), \
         patch("app.intraday_advisory.runtime._discipline", return_value=[]), \
         patch("app.intraday_advisory.runtime._latest_sector_snapshot", return_value=None), \
         patch("app.intraday_advisory.runtime._status", return_value=None), \
         patch("app.intraday_advisory.runtime._drain", new=AsyncMock(side_effect=lambda *_: calls.append("alert") or {"sent": 1})), \
         patch("app.intraday_advisory.runtime._analyze", new=AsyncMock(side_effect=lambda *_a, **_k: calls.append("codex") or {"status": "completed"})):
        first = await run_intraday_advisory_cycle(deps, state, now=MONDAY)
        assert first["events"] >= 1
        assert calls == ["alert"]
        deps = IntradayAdvisoryDependencies(**{**deps.__dict__, "now": lambda: MONDAY + timedelta(seconds=46)})
        await run_intraday_advisory_cycle(deps, state, now=MONDAY + timedelta(seconds=46))
    assert calls[0] == "alert" and 'codex' not in calls


def test_quote_success_evidence_survives_intermediate_idle_ticks() -> None:
    asyncio.run(_quote_success_evidence_survives_intermediate_idle_ticks())


async def _quote_success_evidence_survives_intermediate_idle_ticks() -> None:
    scope = AdvisoryScope("citics-primary", (ScopeItem("600000.SH", "浦发银行", "recommendation", {}),),
                          "snapshot", "decision", ())
    state = RuntimeState(last_deepseek=MONDAY, last_codex=MONDAY, pressure_day=MONDAY.date())
    statuses: list[dict] = []

    async def run_database(call):
        return call()

    fetch = AsyncMock(return_value=[{
        "ts_code": "600000.SH", "name": "浦发银行", "price": 10.1, "pre_close": 10,
        "cumulative_amount": 1_000_000, "cumulative_volume_lot": 1_000,
        "outer_volume_lot": 550, "inner_volume_lot": 450,
        "trade_time": MONDAY.strftime("%Y%m%d%H%M%S"),
    }])
    deps = IntradayAdvisoryDependencies(
        database=object(), run_database=run_database, fetch_quotes=fetch,
        fetch_indices=AsyncMock(return_value={}),
        post_text=AsyncMock(return_value={"status": "sent"}),
        post_card=AsyncMock(return_value={"status": "sent"}),
        session_open=AsyncMock(return_value=(True, "open")), now=lambda: MONDAY,
        account_key=lambda: "citics-primary",
    )
    with patch("app.intraday_advisory.runtime._scope", return_value=scope), \
         patch("app.intraday_advisory.runtime._persist_rows", return_value=1), \
         patch("app.intraday_advisory.runtime._recent_quotes", return_value=[]) as recent, \
         patch("app.intraday_advisory.runtime._discipline", return_value=[]), \
         patch("app.intraday_advisory.runtime._latest_sector_snapshot", return_value=None), \
         patch("app.intraday_advisory.runtime._status",
               side_effect=lambda _database, **values: statuses.append(values)):
        first = await run_intraday_advisory_cycle(deps, state, now=MONDAY)
        second = await run_intraday_advisory_cycle(deps, state, now=MONDAY + timedelta(seconds=1))
    assert fetch.await_count == 1
    assert recent.call_count == 1
    assert first["quote_evidence"]["fresh"] == 1
    assert second["state"] == "idle"
    assert second["quote_evidence"] == first["quote_evidence"]
    assert statuses[-1]["details"]["quote_evidence"]["success_at"] == MONDAY.isoformat()


def test_feishu_env_manager_exposes_advisory_cadence(tmp_path: Path) -> None:
    helper = Path(__file__).parents[2] / "deploy" / "intraday-edge" / "manage_feishu_env.py"
    env_file = tmp_path / "runtime.env"
    payload = json.dumps({"transport": "custom_bot",
                          "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test",
                          "discipline_alert_account_key": "citics-primary"})
    completed = subprocess.run([sys.executable, str(helper), "configure", "--env-file", str(env_file)],
                               input=payload, text=True, capture_output=True, check=True)
    status = json.loads(completed.stdout)
    assert status["intraday_advisory_enabled"]
    assert status["intraday_advisory_cadence"] == {
        "fetch_seconds": 5, "local_tick_seconds": 1, "deepseek_seconds": 600, "codex_seconds": None,
        "briefing_times": ["10:00", "11:35", "14:45"], "event_model_followup": False,
        "notification_policy": "notice-v2",
    }


def test_migration_and_composition_are_declared() -> None:
    root = Path(__file__).parents[2]
    migration = (root / "quant-service" / "migrations" / "versions" /
                 "20260921_0110_intraday_advisory.py").read_text(encoding="utf-8")
    cards = (root / "quant-service" / "migrations" / "versions" /
             "20260921_0111_intraday_advisory_cards.py").read_text(encoding="utf-8")
    market_scope = (root / "quant-service" / "migrations" / "versions" /
                    "20260921_0112_intraday_market_scope.py").read_text(encoding="utf-8")
    main = (root / "quant-service" / "app" / "main.py").read_text(encoding="utf-8")
    assert "intraday_advisory_analysis_runs" in migration
    assert "message_card" in cards
    assert "market_index" in market_scope and "sector" in market_scope
    assert '"intraday_advisory": advisory_enabled' in main
    assert "build_intraday_advisory_router" in main
