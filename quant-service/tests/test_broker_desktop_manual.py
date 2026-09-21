import copy
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.broker_desktop_evidence import account_fingerprint, load_manual_envelope, verify_account_binding
from app.broker_manual_sync import (validate_manual_request, verify_readback, start_manual,
                                    confirm_manual_account, reusable_account_binding)
from app.broker_snapshot_freshness import broker_freshness
from app.personal_decision_contracts import BrokerPortfolioSnapshotInput
from app.personal_decision_repository import persist_broker_snapshot


class DesktopManualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 9, 13, 4, 0, tzinfo=timezone.utc)
        self.run = {"run_id": "run-1", "input_summary": {"account_key": "selected-account"}}
        image = self.root / "holdings.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        self.value = {
            "schema_version": "ths-desktop-holdings-v1", "trigger": "manual", "source": "ths_desktop_ui",
            "run_id": "run-1", "account_key": "selected-account", "observed_at": self.now.isoformat(),
            "trade_date": "2026-09-11",
            "account_identity": {"broker": "verified broker", "masked_account": "****1234", "account_fingerprint": "1" * 64},
            "account_binding": {"method": "existing_account_evidence_match", "existing_snapshot_id": "old-snapshot"},
            "extraction": {"method": "computer_use_verified"},
            "completeness": {"all_positions_visible": True, "position_count": 2, "holdings_pages": [1],
                             "end_of_list_verified": True, "account_totals_verified": True},
            "evidence": [{"path": str(image), "sha256": sha256(image.read_bytes()).hexdigest(),
                          "captured_at": self.now.isoformat(), "kind": "screenshot", "page": 1,
                          "roles": ["account_identity", "account_totals", "holdings"]}],
            "account": {"cash": "100", "total_asset": "800", "total_market_value": "600"},
            "positions": [
                {"symbol": "600000.SH", "name": "测试甲", "quantity": "20", "sellable_quantity": "10",
                 "average_cost": "9", "market_price": "10", "market_value": "200", "unrealized_pnl": "20",
                 "metadata": {"source_page": 1, "visually_verified": True}},
                {"symbol": "000001.SZ", "name": "测试乙", "quantity": "20", "sellable_quantity": "20",
                 "average_cost": "21", "market_price": "20", "market_value": "400", "unrealized_pnl": "-20",
                 "metadata": {"source_page": 1, "visually_verified": True}},
            ],
        }

    def load(self, now=None):
        path = self.root / "envelope.json"
        path.write_text(json.dumps(self.value), encoding="utf-8")
        return load_manual_envelope(path, self.run, now=now or self.now)

    def test_weekend_ui_import_needs_no_market_quote(self):
        snapshot = self.load()
        self.assertEqual(snapshot.metadata["trade_date"], "2026-09-11")
        self.assertEqual(snapshot.observed_at, self.now)
        self.assertEqual(len(snapshot.positions), 2)

    def test_negative_broker_average_cost_is_preserved_as_a_fact(self):
        self.value["positions"][0]["average_cost"] = "-6.0287"
        snapshot = self.load()
        self.assertEqual(snapshot.positions[0].average_cost, Decimal("-6.0287"))

    def test_old_capture_remains_old_on_reimport(self):
        snapshot = self.load(self.now + timedelta(days=7))
        self.assertEqual(snapshot.observed_at, self.now)
        self.assertFalse(broker_freshness(snapshot.model_dump(), self.now + timedelta(days=7))["current"])

    def test_manual_only_and_explicit_account(self):
        for phase, allowed, account, code in [("midday", True, "a", "MANUAL_ONLY"), ("close", True, "a", "MANUAL_ONLY"),
                                               ("manual", False, "a", "AUTHORIZATION"), ("manual", True, None, "BINDING")]:
            with self.subTest(phase=phase, account=account), self.assertRaisesRegex(ValueError, code):
                validate_manual_request(phase, allowed, account)

    def test_export_not_hand_transcribed_json(self):
        self.value["source"] = "ths_desktop_export"
        with self.assertRaisesRegex(ValueError, "EXPORT_EVIDENCE_REQUIRED"):
            self.load()

    def test_hash_and_observation_rejected(self):
        self.value["evidence"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "HASH_MISMATCH"):
            self.load()
        self.value["evidence"][0]["sha256"] = sha256(Path(self.value["evidence"][0]["path"]).read_bytes()).hexdigest()
        self.value["observed_at"] = (self.now + timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ValueError, "OBSERVATION_TIME"):
            self.load()

    def test_uppercase_sha256_from_windows_receipt_is_accepted(self):
        self.value["evidence"][0]["sha256"] = self.value["evidence"][0]["sha256"].upper()
        snapshot = self.load()
        self.assertEqual(snapshot.metadata["evidence"][0]["sha256"],
                         self.value["evidence"][0]["sha256"].lower())

    def test_missing_row_duplicate_and_bad_numeric(self):
        original = copy.deepcopy(self.value)
        for mutate in [lambda value: value["positions"].pop(),
                       lambda value: value["positions"][1].update(symbol="600000.SH"),
                       lambda value: value["positions"][0].update(quantity="NaN"),
                       lambda value: value["positions"][0].update(sellable_quantity="21"),
                       lambda value: value["positions"][0].update(market_value="201.5"),
                       lambda value: value["positions"][0]["metadata"].update(source_page=2),
                       lambda value: value["account"].update(cash="-1")]:
            self.value = copy.deepcopy(original)
            mutate(self.value)
            with self.assertRaises(ValueError):
                self.load()

    def test_empty_requires_explicit_zero_evidence(self):
        self.value["positions"] = []
        self.value["completeness"]["position_count"] = 0
        with self.assertRaises(ValueError):
            self.load()
        self.value["account"]["total_market_value"] = "0"
        with self.assertRaisesRegex(ValueError, "EMPTY_ACCOUNT_UNPROVEN"):
            self.load()
        self.value["completeness"]["explicit_empty"] = True
        self.assertEqual(self.load().positions, [])

    def test_binding_never_defaults_to_citics(self):
        snapshot = self.load()
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = None
        with self.assertRaisesRegex(ValueError, "ACCOUNT_BINDING_REQUIRED"):
            verify_account_binding(connection, snapshot.account_key, snapshot.metadata)
        connection.execute.return_value.fetchone.return_value = {"metadata": {"account_identity": snapshot.metadata["account_identity"]}}
        verify_account_binding(connection, snapshot.account_key, snapshot.metadata)
        mismatch = copy.deepcopy(snapshot.metadata)
        mismatch["account_identity"]["account_fingerprint"] = "2" * 64
        with self.assertRaisesRegex(ValueError, "IDENTITY_MISMATCH"):
            verify_account_binding(connection, snapshot.account_key, mismatch)

    def test_legacy_binding_needs_retained_identity_evidence(self):
        metadata = self.load().metadata
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = {"metadata": {"evidence": metadata["evidence"]}}
        with self.assertRaisesRegex(ValueError, "BINDING_REQUIRED"):
            verify_account_binding(connection, "selected-account", metadata)
        metadata["account_binding"].update(prior_account_identity=metadata["account_identity"],
                                          prior_evidence=metadata["evidence"], identity_comparison="full_account_identifier_match")
        verify_account_binding(connection, "selected-account", metadata)
        metadata["account_binding"]["prior_evidence"][0]["sha256"] = "3" * 64
        with self.assertRaisesRegex(ValueError, "HASH_MISMATCH"):
            verify_account_binding(connection, "selected-account", metadata)

    def test_direct_repository_write_rechecks_files_and_bundle(self):
        snapshot = self.load()
        Path(snapshot.metadata["evidence"][0]["path"]).write_bytes(b"changed")
        connection = Mock()
        with self.assertRaisesRegex(ValueError, "HASH_MISMATCH"):
            persist_broker_snapshot(connection, snapshot)
        connection.execute.assert_not_called()

    def test_readback_rejects_values_and_owner_metadata_mismatch(self):
        snapshot = self.load()
        readback = snapshot.model_dump(mode="json") | {"snapshot_id": "saved"}
        verify_readback(snapshot, {"snapshot_id": "saved"}, readback)
        readback["positions"][0]["quantity"] = "99"
        with self.assertRaisesRegex(ValueError, "POSITION_MISMATCH"):
            verify_readback(snapshot, {"snapshot_id": "saved"}, readback)

    def test_freshness_uses_explicit_trade_date_and_original_policy(self):
        portfolio = self.load().model_dump()
        self.assertTrue(broker_freshness(portfolio, self.now, date(2026, 9, 11))["current"])
        self.assertFalse(broker_freshness(portfolio, self.now, date(2026, 9, 14))["current"])
        self.assertEqual(broker_freshness(portfolio, self.now)["subsequent_trades"], "unknown")

    def test_mask_format_does_not_change_identity(self):
        snapshot = self.load()
        prior = dict(snapshot.metadata["account_identity"], masked_account="12••••34")
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = {"metadata": {"account_identity": prior}}
        verify_account_binding(connection, snapshot.account_key, snapshot.metadata)
        self.assertEqual(account_fingerprint(" Broker ", "００12 34"), account_fingerprint("broker", "001234"))

    def test_manual_start_existing_account_and_concurrency(self):
        connection = Mock()
        connection.transaction.return_value.__enter__ = Mock(return_value=None)
        connection.transaction.return_value.__exit__ = Mock(return_value=False)
        # lock, existing snapshot, currently running manual import
        connection.execute.side_effect = [Mock(), Mock(fetchone=Mock(return_value={"snapshot_id": "old"})),
                                          Mock(fetchone=Mock(return_value={"run_id": "busy", "started_at": self.now}))]
        result = start_manual(connection, "selected-account", self.root, now=self.now)
        self.assertEqual(result["status"], "skipped_running")

    def test_reusable_account_binding_preserves_original_confirmation_anchor(self):
        existing = {
            "snapshot_id": "latest-snapshot",
            "metadata": {"account_binding": {"method": "user_confirmed_once",
                                                "existing_snapshot_id": "confirmation-anchor",
                                                "confirmation_run_id": "confirmation-run"}},
        }
        self.assertEqual(reusable_account_binding(existing), {
            "method": "user_confirmed_once",
            "existing_snapshot_id": "confirmation-anchor",
            "confirmation_run_id": "confirmation-run",
        })

    def test_source_key_cannot_refresh_same_artifact(self):
        snapshot = self.load()
        snapshot.source_snapshot_key = "different-key"
        with self.assertRaisesRegex(ValueError, "BUNDLE_MISMATCH"):
            persist_broker_snapshot(Mock(), snapshot)

    def confirmed_metadata(self, masked=None):
        self.value["account_identity"].update(account_fingerprint=None, masked_account=masked,
                                               identifier_visibility="masked" if masked else "redacted")
        self.value["account_binding"].update(method="user_confirmed_once", confirmation_run_id="run-1")
        self.value["extraction"] = {"method": "desktop_visual_verified", "capture_method": "win32_hwnd"}
        return self.load().metadata

    def confirmation_connection(self, metadata, confirmation=None):
        connection = Mock()
        run = {"run_id": "run-1", "task_key": "broker_holdings_manual", "status": "running",
               "started_at": self.now - timedelta(minutes=1),
               "input_summary": {"account_key": "selected-account"}}
        if confirmation:
            run["input_summary"]["account_confirmation"] = confirmation
        def execute(sql, params=()):
            if "FROM quant.broker_portfolio_snapshots" in sql:
                return Mock(fetchone=Mock(return_value={"snapshot_id": "old-snapshot", "metadata": {}}))
            if "FROM quant.automation_runs" in sql:
                return Mock(fetchone=Mock(return_value=run))
            return Mock()
        connection.execute.side_effect = execute
        connection.transaction.return_value.__enter__ = Mock(return_value=None)
        connection.transaction.return_value.__exit__ = Mock(return_value=False)
        return connection, run

    def test_redacted_identity_requires_server_confirmation_not_envelope_boolean(self):
        metadata = self.confirmed_metadata()
        metadata["account_binding"]["user_confirmed"] = True
        connection, _ = self.confirmation_connection(metadata)
        with self.assertRaisesRegex(ValueError, "USER_CONFIRMATION_REQUIRED"):
            verify_account_binding(connection, "selected-account", metadata)

    def test_confirmation_records_actual_message_and_binds_bundle(self):
        metadata = self.confirmed_metadata()
        connection, run = self.confirmation_connection(metadata)
        record = {"schema_version": "broker-account-confirmation-v1", "source": "current_user_message",
                  "actor": "user", "message_ref": "thread/message-1", "confirmed_at": self.now.isoformat(),
                  "text": "确认这次截图账户映射至 selected-account。", "account_key": "selected-account",
                  "run_id": "run-1", "evidence_bundle_sha256": metadata["evidence_bundle_sha256"]}
        path = self.root / "confirmation.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        receipt = confirm_manual_account(connection, run, self.root / "envelope.json", path, now=self.now)
        self.assertEqual(receipt["status"], "account_confirmed")
        update = next(call.args[1] for call in connection.execute.call_args_list if call.args[0].startswith("UPDATE"))
        stored = update[0].obj["account_confirmation"]
        self.assertEqual(stored["text"], record["text"])
        self.assertIsNone(stored["account_identity"]["account_fingerprint"])
        connection, _ = self.confirmation_connection(metadata, stored)
        verify_account_binding(connection, "selected-account", metadata)
        changed = copy.deepcopy(metadata)
        changed["evidence_bundle_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "USER_CONFIRMATION_REQUIRED"):
            verify_account_binding(connection, "selected-account", changed)

    def test_visible_mask_can_reuse_confirmed_mapping_but_not_black_block(self):
        metadata = self.confirmed_metadata("88****6301")
        confirmation = {"account_key": "selected-account", "existing_snapshot_id": "old-snapshot",
                        "evidence_bundle_sha256": "previous-bundle", "account_identity": metadata["account_identity"],
                        "source": "current_user_message", "message_ref": "thread/message-1", "text": "确认账户映射",
                        "confirmed_at": self.now.isoformat()}
        connection, _ = self.confirmation_connection(metadata, confirmation)
        verify_account_binding(connection, "selected-account", metadata)
        metadata["account_identity"]["masked_account"] = None
        metadata["account_identity"]["identifier_visibility"] = "redacted"
        with self.assertRaisesRegex(ValueError, "USER_CONFIRMATION_REQUIRED"):
            verify_account_binding(connection, "selected-account", metadata)

    def test_native_capture_method_must_not_claim_computer_use(self):
        self.confirmed_metadata()
        self.value["extraction"]["capture_method"] = "unknown"
        with self.assertRaisesRegex(ValueError, "CAPTURE_METHOD"):
            self.load()

    def test_user_confirmation_rejects_wrong_capture_and_agent_actor(self):
        metadata = self.confirmed_metadata()
        record = {"schema_version": "broker-account-confirmation-v1", "source": "current_user_message",
                  "actor": "user", "message_ref": "thread/message-1", "confirmed_at": self.now.isoformat(),
                  "text": "确认这次截图账户映射至 selected-account。", "account_key": "selected-account",
                  "run_id": "run-1", "evidence_bundle_sha256": metadata["evidence_bundle_sha256"]}
        for field, value, error in [("actor", "assistant", "RECORD_REQUIRED"),
                                     ("evidence_bundle_sha256", "wrong", "CAPTURE_MISMATCH"),
                                     ("account_key", "other-account", "CAPTURE_MISMATCH"),
                                     ("confirmed_at", (self.now + timedelta(hours=1)).isoformat(), "TIME_INVALID")]:
            connection, run = self.confirmation_connection(metadata)
            path = self.root / "confirmation.json"
            path.write_text(json.dumps(record | {field: value}), encoding="utf-8")
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, error):
                confirm_manual_account(connection, run, self.root / "envelope.json", path, now=self.now)
            connection.execute.assert_not_called()

    def test_confirmation_idempotency_lock_and_immutable_conflict(self):
        metadata = self.confirmed_metadata()
        connection, run = self.confirmation_connection(metadata)
        record = {"schema_version": "broker-account-confirmation-v1", "source": "current_user_message",
                  "actor": "user", "message_ref": "thread/message-1", "confirmed_at": self.now.isoformat(),
                  "text": "确认这次截图账户映射至 selected-account。", "account_key": "selected-account",
                  "run_id": "run-1", "evidence_bundle_sha256": metadata["evidence_bundle_sha256"]}
        path = self.root / "confirmation.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        confirm_manual_account(connection, run, self.root / "envelope.json", path, now=self.now)
        self.assertTrue(any("FOR UPDATE" in call.args[0] for call in connection.execute.call_args_list))
        stored = next(call.args[1][0].obj["account_confirmation"] for call in connection.execute.call_args_list if call.args[0].startswith("UPDATE"))
        connection, run = self.confirmation_connection(metadata, stored)
        run["status"] = "completed"
        self.assertEqual(confirm_manual_account(connection, run, self.root / "envelope.json", path, now=self.now)["status"],
                         "account_confirmation_idempotent")
        self.assertFalse(any(call.args[0].startswith("UPDATE") for call in connection.execute.call_args_list))
        record["text"] = "换成另一个确认，不允许覆盖原审计。"
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "IMMUTABLE_CONFLICT"):
            confirm_manual_account(connection, run, self.root / "envelope.json", path, now=self.now)

    def test_cross_run_confirmation_cannot_change_account_or_invent_stable_identity(self):
        metadata = self.confirmed_metadata()
        confirmation = {"account_key": "selected-account", "existing_snapshot_id": "old-snapshot",
                        "evidence_bundle_sha256": metadata["evidence_bundle_sha256"],
                        "account_identity": metadata["account_identity"],
                        "source": "current_user_message", "message_ref": "thread/message-1", "text": "确认账户映射",
                        "confirmed_at": self.now.isoformat()}
        # Another manual run can reference exactly the same original capture.
        self.run["run_id"] = self.value["run_id"] = "run-2"
        metadata = self.load().metadata
        connection, _ = self.confirmation_connection(metadata, confirmation)
        verify_account_binding(connection, "selected-account", metadata)
        with self.assertRaisesRegex(ValueError, "IDENTITY_MISMATCH"):
            verify_account_binding(connection, "other-account", metadata)
        metadata["evidence_bundle_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "USER_CONFIRMATION_REQUIRED"):
            verify_account_binding(connection, "selected-account", metadata)


if __name__ == "__main__":
    unittest.main()
