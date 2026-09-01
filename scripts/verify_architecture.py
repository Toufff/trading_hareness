#!/usr/bin/env python3
"""Fast, dependency-free architecture regression guard for local CI/agents."""

from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "quant-service" / "app"
TESTS = ROOT / "quant-service" / "tests"
FRONTEND = ROOT / "frontend" / "src"


def main() -> int:
    problems: list[str] = []
    if not (ROOT / "docs" / "ARCHITECTURE.md").is_file():
        problems.append("missing docs/ARCHITECTURE.md")
    index_check = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_architecture_index.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if index_check.returncode:
        problems.append(index_check.stdout.strip() or "architecture index verification failed")
    for relative in (
        "frontend/src/api/http.ts", "frontend/src/composables/usePolling.ts",
        "frontend/src/composables/useDashboardWorkspace.ts", "frontend/src/dashboard-context.ts",
        "frontend/src/components/RealtimeServicesPanel.vue", "frontend/src/views/ManualRelayView.vue",
        "frontend/src/views/GroupRelayMonitorView.vue", "frontend/src/views/FeishuWorkbenchView.vue",
        "frontend/src/views/research/ResearchOverviewTab.vue", "frontend/src/views/research/MarketSnapshotsTab.vue",
        "frontend/src/views/research/CloseReviewTab.vue", "frontend/src/views/research/StrategyTab.vue",
        "frontend/src/views/research/FactorLabTab.vue", "frontend/src/views/research/StockStudyTab.vue",
        "frontend/src/views/research/AnalystEvidenceTab.vue", "frontend/src/views/research/ClaimReviewTab.vue",
        "frontend/src/views/research/ProviderTab.vue", "frontend/src/views/research/CatalogTab.vue",
        "frontend/src/views/research/QualityTab.vue",
    ):
        if not (ROOT / relative).is_file():
            problems.append(f"missing {relative}")

    # Keep the decompositions from silently regressing.  App.vue is the shell;
    # dashboard state belongs in its composable and feature UI in tab views.
    app_vue_lines = len((FRONTEND / "App.vue").read_text(encoding="utf-8").splitlines())
    if app_vue_lines > 150:
        problems.append(f"frontend/src/App.vue exceeds shell budget: {app_vue_lines} > 150")
    oversized_tests = [
        f"{path.name}:{len(path.read_text(encoding='utf-8').splitlines())}"
        for path in sorted(TESTS.glob("test_*.py"))
        if len(path.read_text(encoding="utf-8").splitlines()) > 1_500
    ]
    if oversized_tests:
        problems.append("oversized focused test module(s): " + ", ".join(oversized_tests))
    for legacy_name in ("test_provider_helpers.py",):
        if (TESTS / legacy_name).exists():
            problems.append(f"legacy catch-all test remains: {legacy_name}")

    main_path = APP / "main.py"
    main_tree = ast.parse(main_path.read_text(encoding="utf-8"))
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
        tree = ast.parse(path.read_text(encoding="utf-8"))
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
