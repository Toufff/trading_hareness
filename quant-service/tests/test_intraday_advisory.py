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

from app.intraday_advisory.rules import QuoteSample, evaluate
from app.intraday_advisory.schedule import decide
from app.intraday_advisory.scope import AdvisoryScope, ScopeItem
from app.intraday_advisory.runtime import (
    IntradayAdvisoryDependencies, RuntimeState, _context, run_intraday_advisory_cycle,
)


TZ = ZoneInfo("Asia/Shanghai")
MONDAY = datetime(2026, 9, 21, 10, 0, tzinfo=TZ)


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
    assert regular.fetch_quotes and regular.run_deepseek and regular.run_codex
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
