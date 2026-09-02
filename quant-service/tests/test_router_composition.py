"""Every router factory under app/routers must be mounted by the composition root."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


APP = Path(__file__).parents[1] / "app"
ROUTERS = APP / "routers"
MAIN = APP / "main.py"


def _router_factories() -> dict[str, list[str]]:
    factories: dict[str, list[str]] = {}
    for path in sorted(ROUTERS.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = [
            node.name for node in tree.body
            if isinstance(node, ast.FunctionDef) and re.fullmatch(r"build_\w+_router", node.name)
        ]
        factories[path.stem] = names
    return factories


class RouterCompositionTests(unittest.TestCase):
    def setUp(self):
        self.factories = _router_factories()
        self.main_source = MAIN.read_text(encoding="utf-8")

    def test_every_router_module_defines_one_factory(self):
        missing = [module for module, names in self.factories.items() if not names]
        self.assertEqual(missing, [], f"router modules without a build_*_router factory: {missing}")
        self.assertGreaterEqual(len(self.factories), 40)

    def test_every_router_factory_is_imported_and_included_by_main(self):
        imported: set[tuple[str, str]] = set()
        for node in ast.walk(ast.parse(self.main_source)):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and (node.module or "").startswith("routers."):
                imported.update((node.module.split(".", 1)[1], alias.name) for alias in node.names)
        not_imported = []
        not_included = []
        for module, names in self.factories.items():
            for name in names:
                if (module, name) not in imported:
                    not_imported.append(f"{module}.{name}")
                if not re.search(rf"app\.include_router\(\s*{name}\(", self.main_source):
                    not_included.append(f"{module}.{name}")
        self.assertEqual(not_imported, [], f"router factories not imported by main.py: {not_imported}")
        self.assertEqual(not_included, [], f"router factories not included by main.py: {not_included}")

    def test_main_does_not_include_unknown_routers(self):
        known = {name for names in self.factories.values() for name in names}
        included = set(re.findall(r"app\.include_router\(\s*(build_\w+_router)\(", self.main_source))
        self.assertEqual(included - known, set())
        self.assertEqual(len(included), len(known))


if __name__ == "__main__":
    unittest.main()
