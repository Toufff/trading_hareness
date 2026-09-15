from app.short_term_lanes.decision_projection import project


def plan():
    return [{"symbol": "002185.SZ"}, {"symbol": "600498.SH"}]


def dossier(symbol, status, created="2026-09-15T08:10:00+00:00", role="candidate"):
    return {"symbol": symbol, "name": symbol, "status": status, "created_at": created,
            "evidence_snapshot": {"role": role}, "gates": []}


def test_projection_requires_every_planned_symbol_to_reach_terminal_state():
    result = project(plan(), [dossier("002185.SZ", "passed"), dossier("600498.SH", "rejected")])
    assert result["status"] == "complete"
    assert result["terminal"] == 2
    assert result["missing_symbols"] == []


def test_incomplete_is_explicit_and_holdings_never_fill_candidate_coverage():
    result = project(plan(), [
        dossier("002185.SZ", "incomplete"),
        dossier("600498.SH", "passed", role="holding"),
    ])
    assert result["status"] == "not_run"
    assert result["terminal"] == 0
    assert result["incomplete"] == 1
    assert result["missing_symbols"] == ["600498.SH"]
