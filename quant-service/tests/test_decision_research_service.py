from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.decision_research_contracts import DecisionResearchDossier, GATE_LABELS, ResearchGate
from app.decision_research_service import _holding_plan, build_dossier, independent_downside_gate


def candidate_evidence() -> dict:
    vendor = [None] * 63
    vendor[4] = "算力、服务器"
    return {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "rank": 1,
        "symbol": "600000.SH",
        "name": "测试股份",
        "candidate_type": "base_ready_30d",
        "score": 82.5,
        "structure": {"metrics": {
            "support_price": 9.40, "resistance_price": 10.00, "sma20": 9.75,
            "recent_range_pct": 3.2,
        }},
        "board_context": {
            "sector_key": "881001", "label": "计算机设备", "net_amount": 800_000_000,
            "flow_percentile": 0.82, "exact_member_mapping": True,
        },
        "daily_basic": {"close": 9.92, "turnover_rate": 5.2, "volume_ratio": 1.3, "pe": 28, "pb": 3.2},
        "flow_raw": {"vendor_row": vendor},
        "main_net_amount": 25_000_000,
        "amount": 650_000_000,
        "close": 9.92,
        "risk_flags": [],
    }


def test_complete_market_structure_candidate_closes_all_gates_and_is_executable():
    dossier = build_dossier(candidate_evidence(), as_of_date=date(2026, 9, 1), holding=False)
    assert dossier.status == "passed"
    assert [gate.gate_key for gate in dossier.gates] == [f"G{index}" for index in range(8)]
    g6 = next(gate for gate in dossier.gates if gate.gate_key == "G6")
    assert g6.verdict == "pass"
    assert g6.independent_run is True
    assert g6.evidence["independence_boundary"] == "bullish score and candidate rank were not inputs"
    assert dossier.evidence_snapshot["geometry"]["reward_risk_1"] > 1


def test_negative_board_flow_is_a_terminal_rejection_not_an_unfinished_placeholder():
    evidence = candidate_evidence()
    evidence["board_context"]["net_amount"] = -10_000_000
    dossier = build_dossier(evidence, as_of_date=date(2026, 9, 1), holding=False)
    assert dossier.status == "rejected"
    assert next(gate for gate in dossier.gates if gate.gate_key == "G3").verdict == "fail"
    assert all(gate.verdict != "unknown" for gate in dossier.gates)


def test_independent_downside_gate_does_not_accept_missing_geometry():
    gate = independent_downside_gate(None, [])
    assert gate.verdict == "unknown"
    assert gate.independent_run is True


def test_passed_contract_rejects_non_independent_g6():
    gates = [ResearchGate(
        gate_key=f"G{index}", label=GATE_LABELS[f"G{index}"], verdict="pass",
        independent_run=False, conclusion="已取得充分证据。", evidence={},
    ) for index in range(8)]
    with pytest.raises(ValueError, match="independent"):
        DecisionResearchDossier(
            dossier_key="test:2026-09-01:600000.SH", as_of_date=date(2026, 9, 1),
            symbol="600000.SH", name="测试股份", strategy_family="short_term_market_structure",
            model_version="test-v1", status="passed", conclusion="研究通过。",
            evidence_snapshot={}, evidence_refs=["test:evidence"], gates=gates,
        )


def test_holding_plan_warning_precedes_stop_and_cap_is_a_policy_limit():
    evidence = candidate_evidence()
    evidence["bars"] = [
        {"close": 9.5 + index * 0.02, "high": 9.7 + index * 0.02, "low": 9.3 + index * 0.02}
        for index in range(20)
    ]
    dossier = build_dossier(
        evidence, as_of_date=date(2026, 9, 1), holding=True,
        position={"position_weight_pct": 81.5},
    )
    plan = _holding_plan(
        dossier, {"position_weight_pct": 81.5},
        datetime(2026, 9, 1, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    warning = float(plan.metadata["risk_warning_price"])
    close = float(dossier.evidence_snapshot["geometry"]["close"])
    assert float(plan.stop_price) < warning < close
    assert plan.max_position_pct == Decimal("25")
    assert plan.metadata["current_position_weight_pct"] == 81.5
