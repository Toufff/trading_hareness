"""Frozen three-arm lifecycle diagnostics; research evidence, never activation.

Callers provide one explicit decision record per arm and event.  A selected
decision only receives a hypothetical return when it also carries the exact
bars and exchange sessions required by the shared effectiveness simulator.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from statistics import mean
from typing import Any

from ..effectiveness.execution import Costs, simulate
from ..effectiveness.rules import numeric
from ..strategy_governance.rules import digest

ARMS = ("current_baseline", "pure_machine", "lifecycle")
VERSION = "trade-thesis-three-arm-v1"
SNAPSHOT_VERSION = "trade-thesis-three-arm-snapshot-v1"


def collect_three_arm_snapshot(scan: dict[str, Any], thesis_receipt: dict[str, Any], *, captured_at: str) -> dict[str, Any]:
    """Freeze decisions already present in one scan; never create a TopN policy."""
    stamp = datetime.fromisoformat(captured_at)
    if stamp.tzinfo is None:
        raise ValueError("timezone-aware captured_at required")
    baseline: dict[str, dict[str, Any]] = {}
    machine: dict[str, dict[str, Any]] = {}
    missing: list[dict[str, str]] = []
    lanes = scan.get("strategy_lanes") if isinstance(scan.get("strategy_lanes"), dict) else scan
    raw_lanes = lanes.get("lanes") if isinstance(lanes, dict) and isinstance(lanes.get("lanes"), list) else lanes
    lane_pairs = ((str(payload.get("key") or payload.get("lane") or "unknown"), payload)
                  for payload in raw_lanes) if isinstance(raw_lanes, list) else (raw_lanes or {}).items()
    for lane, payload in lane_pairs:
        if not isinstance(payload, dict):
            continue
        for section in ("selected", "observation_list", "caution_list"):
            for item in payload.get(section) or []:
                if not isinstance(item, dict) or not item.get("symbol"):
                    continue
                symbol = str(item["symbol"])
                ranking = {key: item.get(key) for key in ("rank", "rank_score", "score", "origin_id") if item.get(key) is not None}
                ranking["lane"] = lane
                machine.setdefault(symbol, {"symbol": symbol, "rankings": []})["rankings"].append(ranking)
                if section == "selected":
                    baseline[symbol] = {"symbol": symbol, "selected": True, "eligibility": item.get("eligibility"),
                                        "action": item.get("action") or "selected_by_existing_scan", "source_section": section,
                                        "lane": lane, "origin_id": item.get("origin_id")}
                elif symbol not in baseline:
                    baseline[symbol] = {"symbol": symbol, "selected": False,
                                        "exclude_reason": item.get("exclude_reason") or f"existing_scan_section:{section}",
                                        "eligibility": item.get("eligibility"), "action": item.get("action"), "lane": lane}
    lifecycle = {}
    for item in thesis_receipt.get("items") or []:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        symbol = str(item["symbol"]); states = item.get("states") or {}
        lifecycle[symbol] = {"symbol": symbol, "selected": False, "eligibility": states.get("entry_state"),
                             "action": "research_only_projection", "exclude_reason": "approved_execution_research_policy_missing",
                             "thesis_id": item.get("thesis_id"), "evaluation_id": item.get("evaluation_id"),
                             "rankings": item.get("current_rankings") or []}
    symbols = sorted(set(baseline) | set(machine) | set(lifecycle))
    arms = {arm: [] for arm in ARMS}
    for symbol in symbols:
        if symbol in baseline:
            arms["current_baseline"].append(baseline[symbol])
        else:
            missing.append({"arm": "current_baseline", "symbol": symbol, "field": "existing_selection_or_exclusion"})
        if symbol in machine:
            entry = machine[symbol]
            entry.update(selected=False, exclude_reason="machine_selection_policy_not_present")
            arms["pure_machine"].append(entry)
        else:
            missing.append({"arm": "pure_machine", "symbol": symbol, "field": "frozen_machine_ranking"})
        if symbol in lifecycle:
            arms["lifecycle"].append(lifecycle[symbol])
        else:
            missing.append({"arm": "lifecycle", "symbol": symbol, "field": "lifecycle_projection"})
    core = {"version": SNAPSHOT_VERSION, "captured_at": captured_at, "source_run_id": thesis_receipt.get("source_run_id"),
            "arms": arms, "missing_fields": missing, "execution_policy": "unavailable_until_governed_policy",
            "live_effect": "none"}
    core["content_hash"] = digest(core)
    return core


def _split(signal_date: str, train_end: str, holdout_end: str) -> str | None:
    day = date.fromisoformat(signal_date)
    if day <= date.fromisoformat(train_end):
        return "train"
    if day <= date.fromisoformat(holdout_end):
        return "holdout"
    return None


def _dedupe(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    for row in records:
        key = (str(row["symbol"]), str(row["event_id"]))
        stamp = datetime.fromisoformat(str(row["available_at"]))
        if stamp.tzinfo is None:
            raise ValueError("timezone-aware available_at required")
        current = chosen.get(key)
        if current is None or (stamp, str(row.get("record_id", ""))) < (
            datetime.fromisoformat(str(current["available_at"])), str(current.get("record_id", ""))
        ):
            chosen[key] = row
    return list(chosen.values()), len(records) - len(chosen)


def _arm_result(row: dict[str, Any], arm: str, costs: Costs) -> dict[str, Any]:
    decision = (row.get("decisions") or {}).get(arm)
    if not isinstance(decision, dict):
        return {"status": "excluded", "reason": "decision_record_missing"}
    if decision.get("selected") is not True:
        reason = decision.get("exclude_reason")
        if not isinstance(reason, str) or not reason:
            return {"status": "excluded", "reason": "explicit_exclude_reason_missing"}
        return {"status": "excluded", "reason": reason}
    policy = decision.get("execution_policy")
    if not isinstance(policy, dict) or policy.get("status") != "approved" or not policy.get("policy_hash"):
        return {"status": "excluded", "reason": "approved_execution_research_policy_missing"}
    contract = decision.get("fill_contract")
    if not isinstance(contract, dict):
        return {"status": "unavailable", "reason": "executable_fill_contract_missing"}
    sessions = contract.get("sessions") or []
    if not sessions or str(sessions[0]) <= str(row["signal_date"]):
        return {"status": "excluded", "reason": "discovery_close_as_entry_forbidden"}
    execution = simulate(contract.get("bars") or [], sessions, costs)
    if execution["status"] != "simulated":
        return {"status": "excluded", "reason": execution["status"], "execution": execution}
    return {"status": "measured", "net_return_pct": execution["net_return_pct"], "execution": execution}


def evaluate_three_arms(records: list[dict[str, Any]], *, train_end: str, holdout_end: str,
                        minimum_holdout: int = 20, costs: Costs = Costs()) -> dict[str, Any]:
    if minimum_holdout < 2:
        raise ValueError("minimum_holdout must be >= 2")
    date.fromisoformat(train_end); date.fromisoformat(holdout_end)
    if train_end >= holdout_end:
        raise ValueError("holdout must follow train")
    cohorts = {str(row.get("cohort_id") or "") for row in records}
    if records and ("" in cohorts or len(cohorts) > 1):
        raise ValueError("one explicit frozen cohort_id required per comparison")
    cohort_id = next(iter(cohorts), "empty_input")
    frozen_hash = digest(records)
    unique, duplicate_count = _dedupe(records)
    arms = {arm: {"eligible": 0, "measured": [], "discovery": [], "exclusions": Counter()} for arm in ARMS}
    event_rows = []
    for row in sorted(unique, key=lambda value: (value["signal_date"], value["symbol"], value["event_id"])):
        split = _split(str(row["signal_date"]), train_end, holdout_end)
        if split is None:
            continue
        cutoff = datetime.fromisoformat(str(row.get("decision_cutoff_at") or row["available_at"]))
        available = datetime.fromisoformat(str(row["available_at"]))
        if available > cutoff:
            for arm in ARMS:
                arms[arm]["exclusions"]["late_available_record"] += 1
            continue
        outcomes = {}
        for arm in ARMS:
            result = _arm_result(row, arm, costs)
            outcomes[arm] = result
            if result["status"] == "measured":
                arms[arm]["eligible"] += 1
                arms[arm]["measured"].append((split, result["net_return_pct"], row["symbol"], row["event_id"]))
            else:
                arms[arm]["exclusions"][result["reason"]] += 1
            discovery = numeric((row.get("discovery_returns") or {}).get(arm))
            if discovery is not None:
                arms[arm]["discovery"].append((split, discovery))
        event_rows.append({"symbol": row["symbol"], "event_id": row["event_id"], "signal_date": row["signal_date"],
                           "split": split, "outcomes": outcomes})
    summaries = {}
    for arm, data in arms.items():
        measured = data["measured"]
        holdout = [value for split, value, *_ in measured if split == "holdout"]
        discovery = [value for split, value in data["discovery"] if split == "holdout"]
        summaries[arm] = {
            "coverage": len(measured) / len(event_rows) if event_rows else 0.0,
            "measured": len(measured), "holdout_measured": len(holdout),
            "holdout_status": "sufficient" if len(holdout) >= minimum_holdout else "insufficient",
            "hypothetical_net_return_pct": mean(holdout) if holdout else None,
            "discovery_return_pct": mean(discovery) if discovery else None,
            "exclusions": dict(data["exclusions"]),
        }
    paired = [row for row in event_rows if row["split"] == "holdout" and
              all(row["outcomes"][arm]["status"] == "measured" for arm in ARMS)]
    paired_summary = {arm: (mean(row["outcomes"][arm]["net_return_pct"] for row in paired) if paired else None) for arm in ARMS}
    findings = []
    for arm in ARMS:
        finding = {"kind": "three_arm_observation", "arm": arm, "holdout_status": summaries[arm]["holdout_status"],
                   "coverage": summaries[arm]["coverage"], "hypothetical_net_return_pct": summaries[arm]["hypothetical_net_return_pct"],
                   "action": "observe_or_propose_only", "live_effect": "none"}
        finding["evidence_hash"] = digest({"input_hash": frozen_hash, **finding})
        findings.append(finding)
    return {"version": VERSION, "status": "completed", "cohort_id": cohort_id, "input_hash": frozen_hash, "costs": costs.__dict__,
            "execution_method": "shared app.effectiveness.execution.simulate", "train_end": train_end,
            "holdout_end": holdout_end, "minimum_holdout": minimum_holdout, "events": len(event_rows),
            "duplicate_events_excluded": duplicate_count, "arms": summaries,
            "paired": {"count": len(paired), "same_sample_net_return_pct": paired_summary},
            "findings": findings, "live_effect": "none",
            "notice": "发现日收益与可成交代理收益分开；未知收益不按零计。样本不足不通过，观察记录不修改策略。"}


__all__ = ["ARMS", "SNAPSHOT_VERSION", "VERSION", "collect_three_arm_snapshot", "evaluate_three_arms"]
