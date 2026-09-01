"""Contracts for terminal, auditable G0-G7 decision research.

The gate labels are human-readable on purpose.  Gate codes remain stable audit
keys, but no user-facing projection needs to remember what ``G4`` means.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


RESEARCH_MODEL_VERSION = "short-term-decision-research-v3"
GateVerdict = Literal["pass", "fail", "unknown", "advisory"]
ResearchStatus = Literal["passed", "rejected", "incomplete"]

GATE_LABELS = {
    "G0": "账户与市场准入",
    "G1": "主营身份与方向归属",
    "G2": "盈利和估值约束",
    "G3": "板块强度与当期催化",
    "G4": "业务与方向映射",
    "G5": "价格结构、流动性与触发条件",
    "G6": "独立下行情景",
    "G7": "完整交易计划",
}


class ResearchGate(BaseModel):
    gate_key: Literal["G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7"]
    label: str = Field(min_length=2, max_length=80)
    verdict: GateVerdict
    independent_run: bool = False
    conclusion: str = Field(min_length=3, max_length=1200)
    evidence: dict[str, Any] = Field(default_factory=dict, max_length=80)


class DecisionResearchDossier(BaseModel):
    dossier_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,180}$")
    as_of_date: date
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    name: str = Field(min_length=1, max_length=120)
    strategy_family: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,80}$")
    model_version: str = Field(min_length=3, max_length=120)
    status: ResearchStatus
    conclusion: str = Field(min_length=3, max_length=2000)
    source_candidate_run_id: str | None = None
    source_candidate_rank: int | None = Field(default=None, ge=1)
    evidence_snapshot: dict[str, Any] = Field(default_factory=dict, max_length=120)
    evidence_refs: list[str] = Field(min_length=1, max_length=80)
    gates: list[ResearchGate] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def validate_terminal_state(self) -> "DecisionResearchDossier":
        by_key = {gate.gate_key: gate for gate in self.gates}
        if set(by_key) != set(GATE_LABELS):
            raise ValueError("dossier must contain exactly G0-G7")
        if any(gate.label != GATE_LABELS[gate.gate_key] for gate in self.gates):
            raise ValueError("gate labels must use the canonical human-readable labels")
        g6 = by_key["G6"]
        terminal_pass = all(gate.verdict in {"pass", "advisory"} for gate in self.gates)
        if self.status == "passed" and (not terminal_pass or not g6.independent_run or g6.verdict != "pass"):
            raise ValueError("passed research requires all gates terminal and an independent passing G6")
        if self.status == "rejected" and not any(gate.verdict == "fail" for gate in self.gates):
            raise ValueError("rejected research requires at least one failed gate")
        if self.status == "incomplete" and not any(gate.verdict == "unknown" for gate in self.gates):
            raise ValueError("incomplete research requires at least one unknown gate")
        return self


def terminal_status(gates: list[ResearchGate]) -> ResearchStatus:
    if any(gate.verdict == "fail" for gate in gates):
        return "rejected"
    if any(gate.verdict == "unknown" for gate in gates):
        return "incomplete"
    g6 = next((gate for gate in gates if gate.gate_key == "G6"), None)
    return "passed" if g6 and g6.verdict == "pass" and g6.independent_run else "incomplete"


__all__ = [
    "DecisionResearchDossier", "GATE_LABELS", "RESEARCH_MODEL_VERSION",
    "ResearchGate", "terminal_status",
]
