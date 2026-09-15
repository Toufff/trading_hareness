#!/usr/bin/env python3
"""Deploy the logical Longhu batching fix over the measured peer image.

This is an incident-scoped, rollback-capable deployment.  It replaces only
``app/routers/longhu_reads.py`` and preserves the pool-recovery base image,
service environments, commands, ports, networks and volumes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.request import ProxyHandler, build_opener


DOCKER = [
    "runuser", "-u", "stockpeer", "--", "env",
    "XDG_RUNTIME_DIR=/run/user/1002",
    "DOCKER_HOST=unix:///run/user/1002/docker.sock",
    "docker",
]
ROOT = Path("/home/stockpeer/trading_hareness/deploy/shared-peer")
CONTEXT = Path(__file__).resolve().parent / "longhu-batching-context"
OLD_TAG = "trading-hareness-peer-quant-research:pool-recovery-20260914"
NEW_TAG = "trading-hareness-peer-quant-research:longhu-batching-20260915"
SERVICES = ["quant-research", "quant-research-scheduler"]


def output(command: list[str]) -> str:
    return subprocess.check_output(command, text=True)


def compose() -> list[str]:
    return DOCKER + [
        "compose", "--env-file", str(ROOT / ".env"),
        "-f", str(ROOT / "compose.yaml"),
        "-f", str(ROOT / "compose.intraday-owner.yaml"),
    ]


def without_image(service: dict) -> dict:
    result = dict(service)
    result.pop("image", None)
    return result


def wait_health(port: int, seconds: int = 90) -> None:
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + seconds
    last_error = "not attempted"
    while time.monotonic() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/health", timeout=5) as response:
                payload = json.load(response)
            if response.status == 200 and payload.get("status") == "ok":
                return
            last_error = f"HTTP {response.status}: {payload.get('status')}"
        except Exception as error:  # bounded and recorded without credentials
            last_error = f"{type(error).__name__}: {error}"
        time.sleep(2)
    raise RuntimeError(f"peer health on {port} did not recover: {last_error}")


def main() -> int:
    # Root invokes this helper from /root, which is deliberately unreadable to
    # the rootless Docker user. Compose resolves relative build metadata from
    # the caller's cwd even when all config paths are absolute.
    os.chdir(ROOT)
    router = CONTEXT / "longhu_reads.py"
    dockerfile = CONTEXT / "Dockerfile"
    source = router.read_text(encoding="utf-8")
    compile(source, str(router), "exec")
    required = [
        "for start in range(0, len(requested), 300):",
        "requested[start:start + 300]",
        '"physical_calls": len(statuses)',
    ]
    if not all(marker in source for marker in required):
        raise RuntimeError("candidate router does not implement logical 300-page batching")
    if "one logical gateway request is capped" in source or "max_length=4_000" in source:
        raise RuntimeError("candidate router retains the obsolete logical cap")

    before = json.loads(output(compose() + ["config", "--format", "json"]))
    override_path = ROOT / "compose.intraday-owner.yaml"
    override = override_path.read_text(encoding="utf-8")
    if override.count(OLD_TAG) != 2:
        raise RuntimeError("unexpected peer image anchors; refusing an ambiguous deployment")

    stamp = time.strftime("%Y%m%dT%H%M%S")
    backup = Path("/home/stockpeer/.local/share/trading-hareness/incident-backups") / f"{stamp}-longhu-batching"
    evidence = Path("/home/stockpeer/.local/share/trading-hareness/hotfixes/longhu-batching-20260915")
    backup.mkdir(parents=True, mode=0o700)
    evidence.mkdir(parents=True, exist_ok=True)
    shutil.copy2(override_path, backup / override_path.name)
    output(DOCKER + ["tag", OLD_TAG, "trading-hareness-peer-quant-research:before-longhu-batching-20260915"])
    subprocess.run(DOCKER + ["build", "--network=none", "-t", NEW_TAG, "-f", str(dockerfile), str(CONTEXT)], check=True)
    candidate_image = output(DOCKER + ["image", "inspect", NEW_TAG, "--format", "{{.Id}}"]).strip()

    deployed = False
    try:
        override_path.write_text(override.replace(OLD_TAG, NEW_TAG), encoding="utf-8")
        after = json.loads(output(compose() + ["config", "--format", "json"]))
        for service in SERVICES:
            if without_image(before["services"][service]) != without_image(after["services"][service]):
                raise RuntimeError(f"non-image compose settings changed for {service}")
        subprocess.run(compose() + ["up", "-d", "--no-deps", "--no-build", *SERVICES], check=True)
        deployed = True
        wait_health(15682)
        wait_health(15683)
        verification = json.loads(output([
            "runuser", "-u", "stockpeer", "--", "python3",
            "/home/stockpeer/trading_hareness/scripts/shared-peer/verify-complete-stock-api.py",
        ]))
        if not verification.get("passed"):
            raise RuntimeError("complete stock API verification did not pass")
    except BaseException:
        shutil.copy2(backup / override_path.name, override_path)
        if deployed:
            subprocess.run(compose() + ["up", "-d", "--no-deps", "--no-build", *SERVICES], check=False)
        raise

    receipt = {
        "status": "passed",
        "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "image": NEW_TAG,
        "image_id": candidate_image,
        "services": SERVICES,
        "non_image_compose_unchanged": True,
        "database_tunnel_untouched": True,
        "verification": verification,
        "rollback": str(backup),
    }
    (evidence / "deployment.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
