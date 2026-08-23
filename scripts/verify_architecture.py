#!/usr/bin/env python3
"""Fast, dependency-free architecture regression guard for local CI/agents."""

from __future__ import annotations

import ast
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "quant-service" / "app"


def main() -> int:
    problems: list[str] = []
    if not (ROOT / "docs" / "ARCHITECTURE.md").is_file():
        problems.append("missing docs/ARCHITECTURE.md")
    for relative in (
        "frontend/src/api/http.ts", "frontend/src/composables/usePolling.ts",
        "frontend/src/components/RealtimeServicesPanel.vue", "frontend/src/views/ManualRelayView.vue",
    ):
        if not (ROOT / relative).is_file():
            problems.append(f"missing {relative}")

    main_path = APP / "main.py"
    main_tree = ast.parse(main_path.read_text())
    direct_routes = []
    for node in main_tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name) and decorator.func.value.id == "app"
                    and decorator.func.attr in {"get", "post", "put", "patch", "delete"}):
                direct_routes.append(f"{node.name}:{node.lineno}")
    if direct_routes:
        problems.append("main.py owns HTTP routes: " + ", ".join(direct_routes))

    for path in APP.rglob("*.py"):
        if path == main_path or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (
                node.module == "app.main" or (node.module == "main" and node.level == 1)
            ):
                problems.append(f"production module imports main: {path.relative_to(APP)}:{node.lineno}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.main":
                        problems.append(f"production module imports main: {path.relative_to(APP)}:{node.lineno}")

    if problems:
        print("Architecture check failed:", *problems, sep="\n- ")
        return 1
    print("Architecture check passed: composition, frontend boundaries and Agent map are present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
