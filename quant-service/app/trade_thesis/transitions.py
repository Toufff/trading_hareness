"""Lifecycle transitions and anti-renewal checks."""
from __future__ import annotations

from typing import Any

from .contracts import AntiRenewalError
from .evidence import parse_time

TERMINAL_STATES = {"invalidated", "expired", "superseded"}


def enforce_no_renewal(thesis: dict[str, Any], previous: dict[str, Any] | None) -> None:
    if not previous:
        return
    prior_deadline = previous.get("terminal_deadline") or (previous.get("thesis") or {}).get("terminal_deadline")
    if prior_deadline and parse_time(thesis["terminal_deadline"], "terminal_deadline") > parse_time(prior_deadline, "previous_terminal_deadline"):
        approved = thesis.get("new_episode_review") or {}
        independent = approved.get("reviewer") and approved.get("author") and approved.get("reviewer") != approved.get("author")
        if not (approved.get("decision") == "approved" and approved.get("incremental_evidence_refs") and
                approved.get("deadline_reason") and independent):
            raise AntiRenewalError("terminal_deadline_extension_requires_independent_evidence_and_review")


def thesis_state(*, cutoff_at: str, deadline: str, invalidated: bool, confirmed: bool,
                 challenged: bool, previous_state: str | None = None, superseded: bool = False) -> str:
    if superseded:
        return "superseded"
    if previous_state in TERMINAL_STATES:
        return previous_state
    if invalidated:
        return "invalidated"
    if parse_time(cutoff_at, "cutoff_at") >= parse_time(deadline, "terminal_deadline"):
        return "expired"
    if challenged:
        return "challenged"
    if confirmed:
        return "supported"
    return previous_state if previous_state in {"supported", "challenged"} else "pending"
