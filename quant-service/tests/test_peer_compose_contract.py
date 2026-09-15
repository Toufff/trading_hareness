from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _services(name: str) -> dict:
    payload = yaml.safe_load((ROOT / "deploy" / "shared-peer" / name).read_text(encoding="utf-8"))
    return payload["services"]


def test_peer_processes_do_not_race_owner_catalog_projection() -> None:
    base = _services("compose.yaml")
    overlay = _services("compose.intraday-owner.yaml")

    assert base["quant-research"]["environment"]["QUANT_CONTROL_PLANE_WRITES_ENABLED"] == "false"
    assert overlay["quant-research-scheduler"]["environment"]["QUANT_CONTROL_PLANE_WRITES_ENABLED"] == "false"


def test_peer_release_provenance_reaches_both_runtime_processes() -> None:
    base = _services("compose.yaml")
    overlay = _services("compose.intraday-owner.yaml")

    for service in (base["quant-research"], overlay["quant-research-scheduler"]):
        environment = service["environment"]
        assert "PEER_APP_GIT_SHA" in environment["APP_GIT_SHA"]
        assert "PEER_APP_RELEASE" in environment["APP_RELEASE"]
        assert "PEER_APP_BUILD_CREATED_AT" in environment["APP_BUILD_CREATED_AT"]


def test_peer_healthcheck_outer_budget_exceeds_application_probe_budget() -> None:
    base = _services("compose.yaml")
    overlay = _services("compose.intraday-owner.yaml")

    for service in (base["quant-research"], overlay["quant-research-scheduler"]):
        healthcheck = service["healthcheck"]
        assert "timeout=15" in " ".join(healthcheck["test"])
        assert healthcheck["timeout"] == "17s"
