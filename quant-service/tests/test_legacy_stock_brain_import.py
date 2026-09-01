"""Tests for the facts-only stock-brain portfolio migration."""

from __future__ import annotations

import json
import importlib.util
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import tempfile
from threading import Thread
import unittest

from app.legacy_stock_brain_import import LegacyPortfolioImportError, load_stock_brain_portfolio


def legacy_payload(evidence_path: Path) -> dict:
    observed_at = "2026-09-01T15:18:09+08:00"
    return {
        "portfolio_observed_at": observed_at,
        "positions": [{
            "code": "sh600664", "name": "哈药股份", "quantity": 12400,
            "available_quantity": 700, "cost": 8.784, "observed_price": 9.49,
            "observed_market_value": 117600, "observed_profit": 8747.61,
            "observed_profit_percent": 8.04, "weight": "81.51%",
            "observed_at": observed_at, "source": "CITIC MuMu readonly UI + deterministic OCR",
            "plan": {"mode": "legacy-plan-that-must-not-migrate"},
            "triggers": [{"action": "legacy-action-that-must-not-migrate"}],
        }, {
            "code": "sz002212", "name": "天融信", "quantity": 3900,
            "available_quantity": 0, "cost": 6.8362, "observed_price": 6.82,
            "observed_market_value": 26598, "observed_profit": -63.05,
            "observed_profit_percent": -0.24, "weight": "18.44%",
            "observed_at": observed_at, "source": "CITIC MuMu readonly UI + deterministic OCR",
            "plan": {}, "triggers": [],
        }],
        "broker_account": {
            "total_assets": 144792.14, "market_value": 144274,
            "available_cash": 437.31, "withdrawable_cash": 437.31,
            "position_percent": 99.64, "observed_at": observed_at,
            "source": "CITIC MuMu readonly UI + deterministic OCR",
            "evidence_path": str(evidence_path),
        },
    }


class LegacyStockBrainImportTests(unittest.TestCase):
    def write_fixture(self, directory: Path, payload: dict) -> Path:
        path = directory / "config.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_converts_exact_broker_facts_and_discards_old_plans(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            evidence = directory / "holdings.png"
            evidence.write_bytes(b"evidence")
            snapshot = load_stock_brain_portfolio(self.write_fixture(directory, legacy_payload(evidence)))

        self.assertEqual(snapshot.verification, "verified_exact")
        self.assertEqual(snapshot.positions[0].symbol, "600664.SH")
        self.assertEqual(snapshot.positions[1].name, "天融信")
        self.assertEqual(snapshot.metadata["market_value_reconciliation_delta"], "76")
        serialized = snapshot.model_dump_json()
        self.assertNotIn("legacy-plan-that-must-not-migrate", serialized)
        self.assertNotIn("legacy-action-that-must-not-migrate", serialized)

    def test_rejects_mixed_timestamp_positions(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            evidence = directory / "holdings.png"
            evidence.write_bytes(b"evidence")
            payload = legacy_payload(evidence)
            payload["positions"][1]["observed_at"] = "2026-08-31T15:00:00+08:00"
            path = self.write_fixture(directory, payload)
            with self.assertRaisesRegex(LegacyPortfolioImportError, "different observation time"):
                load_stock_brain_portfolio(path)

    def test_rejects_missing_broker_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            missing = directory / "missing.png"
            path = self.write_fixture(directory, legacy_payload(missing))
            with self.assertRaisesRegex(LegacyPortfolioImportError, "evidence is missing"):
                load_stock_brain_portfolio(path)

    def test_publish_transport_uses_the_platform_write_key_header(self) -> None:
        received: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
                received["key"] = self.headers.get("X-Quant-Write-Key", "")
                received["authorization"] = self.headers.get("Authorization", "")
                self.rfile.read(int(self.headers.get("content-length", "0")))
                body = b'{"status":"created"}'
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            script_path = Path(__file__).resolve().parents[2] / "scripts" / "import-stock-brain-portfolio.py"
            spec = importlib.util.spec_from_file_location("stock_brain_portfolio_cli", script_path)
            module = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            spec.loader.exec_module(module)
            response = module._request_json(
                f"http://127.0.0.1:{server.server_port}/snapshot",
                method="POST", payload={"ok": True}, token="write-key",
            )
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(response, {"status": "created"})
        self.assertEqual(received["key"], "write-key")
        self.assertEqual(received["authorization"], "")


if __name__ == "__main__":
    unittest.main()
