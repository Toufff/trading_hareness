from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import asyncio
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app.intraday_advisory.rules import AdvisorySignal, QuoteSample, evaluate
from app.intraday_advisory.renderer import analysis_card, signal_card
from app.intraday_advisory.presentation import ensure_readable_card, metric_lines
from app.intraday_advisory.market_watch import index_sample_from_row
from app.intraday_advisory.schedule import decide
from app.intraday_advisory.scope import AdvisoryScope, ScopeItem
from app.intraday_advisory.runtime import (
    IntradayAdvisoryDependencies, RuntimeState, _analyze, _context, _deepseek_push_worthy,
    run_intraday_advisory_cycle,
)


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
    state.pending_events.append({"trigger_price": Decimal("72.50")})

    payload = _context(scope, state, MONDAY, trigger_kind="scheduled", report_kind="ten_minute")

    json.dumps(payload, ensure_ascii=False)
    assert payload["scope"][0]["position_or_recommendation"]["market_price"] == "72.90"
    assert payload["recent_events"][0]["trigger_price"] == "72.50"


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


def test_schedule_assigns_two_ten_minute_slots_to_deepseek_then_one_to_codex() -> None:
    ten = decide(MONDAY.replace(minute=10), last_fetch=MONDAY,
                 last_deepseek=MONDAY, last_codex=MONDAY)
    assert ten.run_deepseek and not ten.run_codex
    twenty = decide(MONDAY.replace(minute=20), last_fetch=MONDAY,
                    last_deepseek=MONDAY.replace(minute=10), last_codex=MONDAY)
    assert twenty.run_deepseek and not twenty.run_codex
    thirty = decide(MONDAY.replace(minute=30), last_fetch=MONDAY,
                    last_deepseek=MONDAY.replace(minute=20), last_codex=MONDAY)
    assert not thirty.run_deepseek and thirty.run_codex
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
    assert "需要关注" in rendered and "当前尚未满足买入条件" in rendered
    assert "quote=null" not in rendered and "breakout_hold" not in rendered
    assert rendered.index("大盘") < rendered.index("持仓关注") < rendered.index("推荐池关注")
    assert "Codex 复核" in rendered


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


def test_market_signal_metrics_are_human_readable() -> None:
    rows = metric_lines({"max_abs_daily_pct": 1.41, "max_abs_60s_pct": 0.36,
                         "affected_indices": 4, "max_abs_snapshot_delta_pct": 0.82,
                         "affected_sectors": 3})
    assert rows == [
        "核心指数最大日内幅度 1.41%", "核心指数最大近1分钟幅度 0.36%", "涉及 4 个核心指数",
        "行业板块相邻快照最大变化 0.82 个百分点", "涉及 3 个行业板块",
    ]


def test_deterministic_delivery_precedes_bundled_codex_analysis() -> None:
    asyncio.run(_deterministic_delivery_precedes_bundled_codex_analysis())


async def _deterministic_delivery_precedes_bundled_codex_analysis() -> None:
    scope = AdvisoryScope("citics-primary", (ScopeItem("600000.SH", "浦发银行", "holding", {}),),
                          "snapshot", "decision", ())
    state = RuntimeState()
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
    assert calls[0] == "alert" and calls[-1] == "codex"


def test_quote_success_evidence_survives_intermediate_idle_ticks() -> None:
    asyncio.run(_quote_success_evidence_survives_intermediate_idle_ticks())


async def _quote_success_evidence_survives_intermediate_idle_ticks() -> None:
    scope = AdvisoryScope("citics-primary", (ScopeItem("600000.SH", "浦发银行", "recommendation", {}),),
                          "snapshot", "decision", ())
    state = RuntimeState(last_deepseek=MONDAY, last_codex=MONDAY)
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
         patch("app.intraday_advisory.runtime._discipline", return_value=[]), \
         patch("app.intraday_advisory.runtime._latest_sector_snapshot", return_value=None), \
         patch("app.intraday_advisory.runtime._status",
               side_effect=lambda _database, **values: statuses.append(values)):
        first = await run_intraday_advisory_cycle(deps, state, now=MONDAY)
        second = await run_intraday_advisory_cycle(deps, state, now=MONDAY + timedelta(seconds=1))
    assert fetch.await_count == 1
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
        "fetch_seconds": 5, "local_tick_seconds": 1, "deepseek_seconds": 600, "codex_seconds": 1800,
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
