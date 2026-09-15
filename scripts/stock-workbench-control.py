#!/usr/bin/env python3
"""Drive the dashboard's ephemeral stock-workbench presentation state.

This tool changes only what the research dashboard displays. It cannot edit
research evidence, trade plans, broker holdings, orders, or database rows.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:5680"
DEFAULT_ENV_FILE = Path(r"G:\StockPlatform\config\runtime.env")
PANEL_NAMES = ("price", "metric", "next_session", "next_week", "messages", "trade_plan")


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def request_json(base_url: str, path: str, *, payload: dict[str, Any] | None, operator_key: str) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if operator_key:
        headers["X-Dashboard-Key"] = operator_key
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}", data=body, headers=headers,
        method="GET" if payload is None else "POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        response_text = error.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(response_text).get("message") or response_text
        except json.JSONDecodeError:
            message = response_text
        raise RuntimeError(f"dashboard returned HTTP {error.code}: {message}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"dashboard is unavailable at {base_url}: {error.reason}") from error


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=os.getenv("STOCK_DASHBOARD_URL", DEFAULT_BASE_URL))
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--workspace-id", default="primary")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control the stock research workbench presentation")
    add_common_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("show", help="read the current presentation state")

    present = subparsers.add_parser("present", help="open or update a stock presentation")
    present.add_argument("--symbol")
    present.add_argument("--lookback-days", type=int)
    present.add_argument("--strategy", choices=(
        "accumulation", "expansion", "pullback", "trend", "event", "relay",
        "contraction", "rotation", "reclaim",
    ))
    present.add_argument("--timeframe", choices=("daily", "weekly"))
    present.add_argument("--metric", choices=("vendor_flow", "volume", "turnover", "macd", "rsi"))
    present.add_argument("--zoom-start", type=float)
    present.add_argument("--zoom-end", type=float)
    present.add_argument("--focus-scope", choices=("next_session", "next_week"))
    present.add_argument("--focus-state")
    present.add_argument("--clear-focus", action="store_true")
    present.add_argument("--show-panel", action="append", choices=PANEL_NAMES, default=[])
    present.add_argument("--hide-panel", action="append", choices=PANEL_NAMES, default=[])
    present.add_argument("--note-title")
    present.add_argument("--note-body")
    present.add_argument("--note-source")
    present.add_argument("--note-as-of")
    present.add_argument("--clear-note", action="store_true")
    present.add_argument("--ttl-seconds", type=int, default=1800)

    annotate = subparsers.add_parser("annotate", help="add or replace a chart/narration annotation")
    annotate.add_argument("--kind", required=True, choices=("price_line", "point", "region", "note"))
    annotate.add_argument("--id")
    annotate.add_argument("--label", required=True)
    annotate.add_argument("--detail")
    annotate.add_argument("--color")
    annotate.add_argument("--source")
    annotate.add_argument("--as-of")
    annotate.add_argument("--price", type=float)
    annotate.add_argument("--date")
    annotate.add_argument("--start-date")
    annotate.add_argument("--end-date")
    annotate.add_argument("--low", type=float)
    annotate.add_argument("--high", type=float)
    annotate.add_argument("--ttl-seconds", type=int, default=1800)

    remove = subparsers.add_parser("remove", help="remove one annotation by id")
    remove.add_argument("annotation_id")
    remove.add_argument("--ttl-seconds", type=int, default=1800)
    subparsers.add_parser("clear", help="clear every agent annotation")
    subparsers.add_parser("reset", help="clear the entire presentation state")
    return parser


def compact(mapping: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in mapping.items() if value is not None}


def command_payload(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.command == "show":
        return None
    common = {"workspace_id": args.workspace_id}
    if args.command == "reset":
        return {**common, "operation": "reset"}
    if args.command == "clear":
        return {**common, "operation": "clear_annotations"}
    if args.command == "remove":
        return {**common, "operation": "remove_annotation", "annotation_id": args.annotation_id, "ttl_seconds": args.ttl_seconds}
    if args.command == "annotate":
        annotation = compact({
            "kind": args.kind, "id": args.id, "label": args.label, "detail": args.detail,
            "color": args.color, "source_label": args.source, "as_of": args.as_of,
            "price": args.price, "date": args.date, "start_date": args.start_date,
            "end_date": args.end_date, "low": args.low, "high": args.high,
        })
        return {**common, "operation": "annotate", "annotation": annotation, "ttl_seconds": args.ttl_seconds}

    panels = {panel: True for panel in args.show_panel}
    panels.update({panel: False for panel in args.hide_panel})
    if args.clear_focus:
        focus: dict[str, str] | None = None
    elif args.focus_scope or args.focus_state:
        if not args.focus_scope or not args.focus_state:
            raise RuntimeError("--focus-scope and --focus-state must be supplied together")
        focus = {"scope": args.focus_scope, "state": args.focus_state}
    else:
        focus = None
    payload = compact({
        **common,
        "operation": "present",
        "symbol": args.symbol,
        "lookback_days": args.lookback_days,
        "strategy_key": args.strategy,
        "timeframe": args.timeframe,
        "metric": args.metric,
        "zoom": compact({"start": args.zoom_start, "end": args.zoom_end}) or None,
        "panel_visibility": panels or None,
        "ttl_seconds": args.ttl_seconds,
    })
    if args.clear_focus or focus is not None:
        payload["focus"] = focus
    if args.clear_note:
        payload["speaker_note"] = None
    elif args.note_title or args.note_body:
        if not args.note_title or not args.note_body:
            raise RuntimeError("--note-title and --note-body must be supplied together")
        payload["speaker_note"] = compact({
            "title": args.note_title, "body": args.note_body,
            "source_label": args.note_source, "as_of": args.note_as_of,
        })
    return payload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env_values = read_env_file(args.env_file)
    operator_key = os.getenv("DASHBOARD_OPERATOR_KEY", "") or env_values.get("DASHBOARD_OPERATOR_KEY", "")
    try:
        payload = command_payload(args)
        result = request_json(args.base_url, "/api/research/workbench-control", payload=payload, operator_key=operator_key)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
