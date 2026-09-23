"""Safely manage direct Feishu alert settings in an edge env file.

Configuration is accepted through stdin so the application secret never has to
appear in a process command line.  Output is deliberately limited to boolean
readiness fields and never contains credential values.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any


ALLOWED_RECEIVE_ID_TYPES = {"chat_id", "open_id", "union_id", "user_id", "email"}
MANAGED_KEYS = (
    "FEISHU_ALERTS_CONFIGURED",
    "FEISHU_ALERT_TRANSPORT",
    "QUANT_FEISHU_DIRECT_ENABLED",
    "FEISHU_CUSTOM_BOT_WEBHOOK_URL",
    "FEISHU_CUSTOM_BOT_SIGNING_SECRET",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_ALERT_RECEIVE_ID",
    "FEISHU_ALERT_RECEIVE_ID_TYPE",
    "INTRADAY_SCAN_INTERVAL_SECONDS",
    "QUANT_DISCIPLINE_ALERTS_ENABLED",
    "QUANT_DISCIPLINE_ALERT_INTERVAL_SECONDS",
    "QUANT_DISCIPLINE_ALERT_ACCOUNT_KEY",
    "QUANT_INTRADAY_ADVISORY_ENABLED",
    "QUANT_INTRADAY_ADVISORY_ACCOUNT_KEY",
    "QUANT_INTRADAY_ADVISORY_FETCH_SECONDS",
    "QUANT_INTRADAY_ADVISORY_LOCAL_TICK_SECONDS",
    "QUANT_INTRADAY_ADVISORY_DEEPSEEK_SECONDS",
    "QUANT_INTRADAY_ADVISORY_CODEX_SECONDS",
    "INTRADAY_ADVISORY_CODEX_MODEL",
    "INTRADAY_ADVISORY_CODEX_REASONING",
)


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _safe_value(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    if "\n" in text or "\r" in text or "\x00" in text:
        raise ValueError(f"{name} contains an unsupported control character")
    return text


def _update_env(path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    output: list[str] = []
    seen: set[str] = set()
    for line in existing_lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0]
            if key in updates:
                if key not in seen:
                    output.append(f"{key}={updates[key]}")
                    seen.add(key)
                continue
        output.append(line)
    for key in MANAGED_KEYS:
        if key in updates and key not in seen:
            output.append(f"{key}={updates[key]}")

    original_stat = path.stat() if path.exists() else None
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(output).rstrip("\n") + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
        if original_stat is not None and hasattr(os, "chown"):
            os.chown(temporary, original_stat.st_uid, original_stat.st_gid)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _status(path: Path) -> dict[str, Any]:
    values = _parse_env(path)
    transport = values.get("FEISHU_ALERT_TRANSPORT", "app") or "app"
    if transport == "custom_bot":
        required_present = bool(values.get("FEISHU_CUSTOM_BOT_WEBHOOK_URL", "").strip())
    else:
        required_present = all(
            bool(values.get(key, "").strip())
            for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_ALERT_RECEIVE_ID")
        )
    receive_type = values.get("FEISHU_ALERT_RECEIVE_ID_TYPE", "chat_id") or "chat_id"
    return {
        "status": "ready" if required_present else "incomplete",
        "enabled": (
            values.get("FEISHU_ALERTS_CONFIGURED", "false").lower() == "true"
            and required_present
            and (
                transport == "custom_bot"
                or values.get("QUANT_FEISHU_DIRECT_ENABLED", "false").lower() == "true"
            )
        ),
        "configured": values.get("FEISHU_ALERTS_CONFIGURED", "false").lower() == "true",
        "credentials_present": required_present,
        "transport": transport,
        "signing_secret_present": bool(values.get("FEISHU_CUSTOM_BOT_SIGNING_SECRET", "").strip()),
        "receive_id_type": receive_type,
        "scan_interval_seconds": int(values.get("INTRADAY_SCAN_INTERVAL_SECONDS", "0") or 0),
        "discipline_alerts_enabled": values.get("QUANT_DISCIPLINE_ALERTS_ENABLED", "false").lower() == "true",
        "discipline_alert_interval_seconds": int(values.get("QUANT_DISCIPLINE_ALERT_INTERVAL_SECONDS", "0") or 0),
        "discipline_alert_account_configured": bool(values.get("QUANT_DISCIPLINE_ALERT_ACCOUNT_KEY", "").strip()),
        "intraday_advisory_enabled": values.get("QUANT_INTRADAY_ADVISORY_ENABLED", "false").lower() == "true",
        "intraday_advisory_account_configured": bool(values.get("QUANT_INTRADAY_ADVISORY_ACCOUNT_KEY", "").strip()),
        "intraday_advisory_cadence": {
            # These are code-owned timings, not the legacy env hints below.
            "fetch_seconds": 5, "local_tick_seconds": 1, "deepseek_seconds": 600,
            "codex_seconds": None, "briefing_times": ["10:00", "11:35", "14:45"],
            "event_model_followup": False, "notification_policy": "notice-v2",
        },
        "env_file": str(path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("configure", "disable", "status"))
    parser.add_argument("--env-file", default="/etc/quant-intraday-edge.env")
    parser.add_argument(
        "--configured-only",
        action="store_true",
        help="store credentials but keep the current host's direct writer disabled",
    )
    args = parser.parse_args()
    path = Path(args.env_file).resolve()

    if args.action == "configure":
        payload = json.load(os.sys.stdin)
        transport = str(payload.get("transport") or "custom_bot").strip()
        if transport not in {"custom_bot", "app"}:
            raise ValueError("transport must be custom_bot or app")
        receive_id_type = str(payload.get("receive_id_type") or "chat_id").strip()
        if receive_id_type not in ALLOWED_RECEIVE_ID_TYPES:
            raise ValueError(
                "receive_id_type must be one of " + ", ".join(sorted(ALLOWED_RECEIVE_ID_TYPES))
            )
        updates = {
            "FEISHU_ALERTS_CONFIGURED": "true",
            "FEISHU_ALERT_TRANSPORT": transport,
            "INTRADAY_SCAN_INTERVAL_SECONDS": "30",
            "QUANT_DISCIPLINE_ALERTS_ENABLED": "true",
            "QUANT_DISCIPLINE_ALERT_INTERVAL_SECONDS": "30",
            "QUANT_DISCIPLINE_ALERT_ACCOUNT_KEY": _safe_value(
                "discipline_alert_account_key", payload.get("discipline_alert_account_key") or "citics-primary"
            ),
            "QUANT_INTRADAY_ADVISORY_ENABLED": "true",
            "QUANT_INTRADAY_ADVISORY_ACCOUNT_KEY": _safe_value(
                "intraday_advisory_account_key", payload.get("discipline_alert_account_key") or "citics-primary"
            ),
            "QUANT_INTRADAY_ADVISORY_FETCH_SECONDS": "5",
            "QUANT_INTRADAY_ADVISORY_LOCAL_TICK_SECONDS": "1",
            "QUANT_INTRADAY_ADVISORY_DEEPSEEK_SECONDS": "600",
            "QUANT_INTRADAY_ADVISORY_CODEX_SECONDS": "1800",
            "INTRADAY_ADVISORY_CODEX_MODEL": "gpt-6-sol",
            "INTRADAY_ADVISORY_CODEX_REASONING": "high",
        }
        if transport == "custom_bot":
            webhook_url = _safe_value("webhook_url", payload.get("webhook_url"))
            if not webhook_url.startswith("https://open.feishu.cn/open-apis/bot/v2/hook/"):
                raise ValueError("webhook_url must be an official Feishu custom-bot webhook URL")
            updates.update(
                {
                    "QUANT_FEISHU_DIRECT_ENABLED": "false",
                    "FEISHU_CUSTOM_BOT_WEBHOOK_URL": webhook_url,
                    "FEISHU_CUSTOM_BOT_SIGNING_SECRET": str(payload.get("signing_secret") or "").strip(),
                    "FEISHU_APP_ID": "",
                    "FEISHU_APP_SECRET": "",
                    "FEISHU_ALERT_RECEIVE_ID": "",
                    "FEISHU_ALERT_RECEIVE_ID_TYPE": "chat_id",
                }
            )
        else:
            updates.update(
                {
                    "QUANT_FEISHU_DIRECT_ENABLED": "false" if args.configured_only else "true",
                    "FEISHU_CUSTOM_BOT_WEBHOOK_URL": "",
                    "FEISHU_CUSTOM_BOT_SIGNING_SECRET": "",
                    "FEISHU_APP_ID": _safe_value("app_id", payload.get("app_id")),
                    "FEISHU_APP_SECRET": _safe_value("app_secret", payload.get("app_secret")),
                    "FEISHU_ALERT_RECEIVE_ID": _safe_value("receive_id", payload.get("receive_id")),
                    "FEISHU_ALERT_RECEIVE_ID_TYPE": receive_id_type,
                }
            )
        _update_env(path, updates)
    elif args.action == "disable":
        _update_env(
            path,
            {
                "FEISHU_ALERTS_CONFIGURED": "false",
                "QUANT_FEISHU_DIRECT_ENABLED": "false",
                "QUANT_DISCIPLINE_ALERTS_ENABLED": "false",
                "QUANT_INTRADAY_ADVISORY_ENABLED": "false",
            },
        )

    print(json.dumps(_status(path), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
