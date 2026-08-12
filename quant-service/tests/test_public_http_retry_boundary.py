"""Prevent public-market adapters from bypassing their bounded retry primitive."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


class _HttpClientCallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.function_stack: list[str] = []
        self.violations: list[tuple[int, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "client":
            if node.func.attr in {"get", "post", "put", "delete", "request"} and self.function_stack[-1:] != ["_request_with_retry"]:
                self.violations.append((node.lineno, node.func.attr))
        self.generic_visit(node)


class PublicHttpRetryBoundaryTests(unittest.TestCase):
    def test_free_market_adapter_uses_only_the_bounded_retry_primitive_for_http(self) -> None:
        source = Path(__file__).resolve().parents[1] / "app" / "free_market_providers.py"
        visitor = _HttpClientCallVisitor()
        visitor.visit(ast.parse(source.read_text()))
        self.assertEqual(visitor.violations, [], f"direct public HTTP calls bypass retry: {visitor.violations}")
