"""Manual desktop broker evidence boundary. No UI, authentication or quote I/O.

Normalized UI observations are accepted only with complete, hashed screenshots.
Export ingestion is deliberately closed until a real client format has a tested
parser; a manually typed JSON document is never itself a client export.
"""
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import unicodedata

from .broker_fact_sync_rules import SHANGHAI, validate_exact_totals

CONTRACT = "ths-desktop-holdings-v1"
SOURCES = {"ths_desktop_export", "ths_desktop_ui"}


def account_fingerprint(broker, full_account_identifier):
    """SHA256 of NFKC/casefold broker + ':' + NFKC account without whitespace.

    The caller visually verifies the complete identifier in both evidence
    sets. This function and file hashes do not perform OCR or establish that
    the caller's transcription is truthful. Never log the complete input.
    """
    normalized_broker = unicodedata.normalize("NFKC", broker).strip().casefold()
    account = "".join(unicodedata.normalize("NFKC", full_account_identifier).split()).upper()
    return sha256((normalized_broker + ":" + account).encode("utf-8")).hexdigest()


def same_identity(left, right):
    left, right = identity(left), identity(right)
    return (unicodedata.normalize("NFKC", left["broker"]).strip().casefold(), left["account_fingerprint"]) == (
        unicodedata.normalize("NFKC", right["broker"]).strip().casefold(), right["account_fingerprint"])


def aware_time(value, label):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("BROKER_TIMEZONE_REQUIRED: " + label)
    return parsed


def identity(value, *, allow_redacted=False):
    value = value or {}
    if not value.get("broker"):
        raise ValueError("ACCOUNT_BINDING_REQUIRED: explicit broker required")
    if allow_redacted and "account_fingerprint" in value and value["account_fingerprint"] is None:
        visibility = value.get("identifier_visibility")
        masked = value.get("masked_account")
        if visibility == "redacted" and masked is None:
            return {"broker": value["broker"], "account_fingerprint": None,
                    "masked_account": None, "identifier_visibility": "redacted"}
        if visibility == "masked" and isinstance(masked, str) and any(char in masked for char in "*•…"):
            return {"broker": value["broker"], "account_fingerprint": None,
                    "masked_account": masked, "identifier_visibility": "masked"}
        raise ValueError("ACCOUNT_BINDING_REQUIRED: preserve actual masked or redacted identity")
    if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("account_fingerprint", ""))):
        raise ValueError("ACCOUNT_BINDING_REQUIRED: broker and account fingerprint required")
    masked = str(value.get("masked_account", ""))
    if not masked or not any(char in masked for char in "*•…"):
        raise ValueError("ACCOUNT_BINDING_REQUIRED: masked account required")
    return {key: value[key] for key in ("broker", "account_fingerprint", "masked_account")}


def reusable_identity_clue(left, right):
    """A confirmed mapping may be reused only with a visible matching clue.

    A broker name, HWND, process id or a wholly redacted block is not an
    account identity. Mask equality is a user-confirmed mapping clue, never
    promoted to full-account verification.
    """
    left, right = identity(left, allow_redacted=True), identity(right, allow_redacted=True)
    broker = lambda value: unicodedata.normalize("NFKC", value["broker"]).strip().casefold()
    if broker(left) != broker(right):
        return False
    if left["account_fingerprint"] and right["account_fingerprint"]:
        return left["account_fingerprint"] == right["account_fingerprint"]
    a, b = left.get("masked_account"), right.get("masked_account")
    return bool(a and b and a == b and sum(char.isdigit() for char in a) >= 4)


def verify_user_confirmation(connection, account_key, metadata):
    binding = metadata["account_binding"]
    row = connection.execute(
        "SELECT run_id,task_key,input_summary FROM quant.automation_runs WHERE run_id=%s",
        (binding.get("confirmation_run_id"),),
    ).fetchone()
    confirmation = ((row or {}).get("input_summary") or {}).get("account_confirmation") or {}
    if (not row or row.get("task_key") != "broker_holdings_manual"
            or confirmation.get("source") != "current_user_message"
            or not all(confirmation.get(key) for key in ("message_ref", "text", "confirmed_at"))):
        raise ValueError("BROKER_USER_CONFIRMATION_REQUIRED: no server-side user confirmation")
    if (confirmation.get("account_key") != account_key
            or (row.get("input_summary") or {}).get("account_key") != account_key
            or str(confirmation.get("existing_snapshot_id")) != str(binding["existing_snapshot_id"])):
        raise ValueError("BROKER_ACCOUNT_IDENTITY_MISMATCH")
    current = identity(metadata.get("account_identity"), allow_redacted=True)
    prior = identity(confirmation.get("account_identity"), allow_redacted=True)
    normalize = lambda value: unicodedata.normalize("NFKC", value).strip().casefold()
    if normalize(current["broker"]) != normalize(prior["broker"]):
        raise ValueError("BROKER_ACCOUNT_IDENTITY_MISMATCH")
    if confirmation.get("evidence_bundle_sha256") == metadata.get("evidence_bundle_sha256"):
        if current != prior:
            raise ValueError("BROKER_ACCOUNT_IDENTITY_MISMATCH: same capture changed identity transcription")
        return
    if not reusable_identity_clue(prior, current):
        raise ValueError("BROKER_USER_CONFIRMATION_REQUIRED: new capture has no stable visible account clue")


def verify_artifacts(items, observed_at=None):
    if not isinstance(items, list) or not items:
        raise ValueError("BROKER_EVIDENCE_MISSING")
    result = []
    for item in items:
        path = Path(item.get("path", ""))
        if not path.is_absolute() or not path.is_file() or path.stat().st_size > 50_000_000:
            raise ValueError("BROKER_EVIDENCE_FILE_INVALID")
        body = path.read_bytes()
        digest = sha256(body).hexdigest()
        if digest != item.get("sha256"):
            raise ValueError("BROKER_EVIDENCE_HASH_MISMATCH")
        if item.get("kind") == "screenshot" and not (body.startswith(b"\x89PNG\r\n\x1a\n") or body.startswith(b"\xff\xd8\xff")):
            raise ValueError("BROKER_SCREENSHOT_INVALID")
        captured = aware_time(item.get("captured_at"), "captured_at")
        if observed_at is not None and not timedelta(0) <= observed_at - captured <= timedelta(minutes=10):
            raise ValueError("BROKER_CAPTURE_OBSERVATION_MISMATCH")
        result.append({**item, "path": str(path.resolve()), "sha256": digest, "captured_at": captured.isoformat()})
    return result


def validate_desktop_metadata(metadata, source, observed_at, positions):
    """Shared DTO validation: does not touch files (HTTP accepts evidence refs)."""
    if metadata.get("contract") != CONTRACT or metadata.get("trigger") != "manual":
        raise ValueError("BROKER_MANUAL_EVIDENCE_REQUIRED")
    binding = metadata.get("account_binding") or {}
    method = binding.get("method")
    if method not in {"existing_account_evidence_match", "user_confirmed_once"} or not binding.get("existing_snapshot_id"):
        raise ValueError("ACCOUNT_BINDING_REQUIRED")
    identity(metadata.get("account_identity"), allow_redacted=method == "user_confirmed_once")
    if method == "user_confirmed_once" and not binding.get("confirmation_run_id"):
        raise ValueError("BROKER_USER_CONFIRMATION_REQUIRED")
    day = date.fromisoformat(str(metadata.get("trade_date", "")))
    if day > observed_at.astimezone(SHANGHAI).date():
        raise ValueError("BROKER_TRADE_DATE_AFTER_OBSERVATION")
    if not re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("evidence_bundle_sha256", ""))):
        raise ValueError("BROKER_EVIDENCE_HASH_REQUIRED")
    completeness = metadata.get("completeness") or {}
    evidence = metadata.get("evidence") or []
    if completeness.get("all_positions_visible") is not True or completeness.get("position_count") != len(positions):
        raise ValueError("BROKER_INCOMPLETE_POSITIONS")
    if not positions and completeness.get("explicit_empty") is not True:
        raise ValueError("BROKER_EMPTY_ACCOUNT_UNPROVEN")
    if source == "ths_desktop_export":
        raise ValueError("BROKER_EXPORT_PARSER_UNAVAILABLE: real client format must be validated first")
    extraction = metadata.get("extraction") or {}
    if extraction.get("method") not in {"computer_use_verified", "desktop_visual_verified", "window_capture_verified"}:
        raise ValueError("BROKER_UI_EXTRACTION_UNVERIFIED")
    if extraction.get("method") in {"desktop_visual_verified", "window_capture_verified"} and extraction.get("capture_method") != "win32_hwnd":
        raise ValueError("BROKER_CAPTURE_METHOD_UNVERIFIED")
    if not evidence or any(item.get("kind") != "screenshot" for item in evidence):
        raise ValueError("BROKER_UI_SCREENSHOTS_REQUIRED")
    roles = {role for item in evidence for role in item.get("roles", [])}
    if not {"account_identity", "account_totals", "holdings"} <= roles:
        raise ValueError("BROKER_UI_COVERAGE_INCOMPLETE")
    pages = completeness.get("holdings_pages") or []
    indices = {item.get("page") for item in evidence if "holdings" in item.get("roles", [])}
    if not pages or pages != list(range(1, len(pages) + 1)) or set(pages) != indices:
        raise ValueError("BROKER_UI_PAGE_COVERAGE_INCOMPLETE")
    if completeness.get("end_of_list_verified") is not True or completeness.get("account_totals_verified") is not True:
        raise ValueError("BROKER_UI_COMPLETENESS_UNVERIFIED")
    for position in positions:
        row = position.model_dump() if hasattr(position, "model_dump") else position
        proof = row.get("metadata") or {}
        if proof.get("source_page") not in indices or proof.get("visually_verified") is not True:
            raise ValueError("BROKER_POSITION_EVIDENCE_MISSING")


def verify_account_binding(connection, account_key, metadata):
    """Bind the supplied client account to an existing backend account fact.

For old snapshots lacking identity metadata, a matched identity must be tied
to a hash already retained in that snapshot's evidence. No THS/CITIC default.
"""
    binding = metadata.get("account_binding") or {}
    existing = connection.execute(
        "SELECT snapshot_id,metadata FROM quant.broker_portfolio_snapshots WHERE account_key=%s AND snapshot_id=%s",
        (account_key, binding.get("existing_snapshot_id")),
    ).fetchone()
    if not existing:
        raise ValueError("ACCOUNT_BINDING_REQUIRED: existing account snapshot not found")
    if binding.get("method") == "user_confirmed_once":
        verify_user_confirmation(connection, account_key, metadata)
        return
    prior = existing.get("metadata") or {}
    current = identity(metadata.get("account_identity"))
    prior_identity = prior.get("account_identity")
    if prior_identity:
        if not same_identity(prior_identity, current):
            raise ValueError("BROKER_ACCOUNT_IDENTITY_MISMATCH")
        return
    if not same_identity(binding.get("prior_account_identity"), current):
        raise ValueError("BROKER_ACCOUNT_IDENTITY_MISMATCH")
    old_evidence = verify_artifacts(binding.get("prior_evidence"))
    if any(item.get("kind") != "screenshot" or "account_identity" not in item.get("roles", []) for item in old_evidence):
        raise ValueError("ACCOUNT_BINDING_REQUIRED: prior screenshot identity evidence required")
    saved_hashes = {item.get("sha256") for item in prior.get("evidence", [])}
    if not saved_hashes or any(item["sha256"] not in saved_hashes for item in old_evidence):
        raise ValueError("ACCOUNT_BINDING_REQUIRED: prior identity evidence not in existing snapshot")
    if binding.get("identity_comparison") != "full_account_identifier_match":
        raise ValueError("ACCOUNT_BINDING_REQUIRED: full identifier comparison required")


def load_manual_envelope(path, run, *, now=None):
    from .personal_decision_contracts import BrokerPortfolioSnapshotInput
    now = now or datetime.now(timezone.utc)
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if value.get("schema_version") != CONTRACT or value.get("trigger") != "manual":
        raise ValueError("BROKER_MANUAL_EVIDENCE_REQUIRED")
    if value.get("source") not in SOURCES or str(value.get("run_id")) != str(run["run_id"]):
        raise ValueError("BROKER_RUN_EVIDENCE_MISMATCH")
    if value.get("account_key") != (run.get("input_summary") or {}).get("account_key"):
        raise ValueError("BROKER_ACCOUNT_IDENTITY_MISMATCH")
    observed = aware_time(value.get("observed_at"), "observed_at")
    if observed > now + timedelta(seconds=5):
        raise ValueError("BROKER_OBSERVATION_TIME_INVALID")
    # Old captures may be reimported as history. Their time is never replaced
    # with start/recorded_at, and advice eligibility remains a separate rule.
    evidence = verify_artifacts(value.get("evidence"), observed)
    hashes = sorted(item["sha256"] for item in evidence)
    bundle_hash = sha256(json.dumps(hashes).encode()).hexdigest()
    metadata = {"contract": CONTRACT, "trigger": "manual", "trade_date": value.get("trade_date"),
                "account_identity": value.get("account_identity"), "account_binding": value.get("account_binding"),
                "extraction": value.get("extraction"), "completeness": value.get("completeness"),
                "evidence": evidence, "evidence_bundle_sha256": bundle_hash,
                "subsequent_trades": "unknown"}
    account = value.get("account") or {}
    positions = value.get("positions")
    if not isinstance(positions, list):
        raise ValueError("BROKER_POSITIONS_MISSING")
    validate_exact_totals({"total_assets": account.get("total_asset"), "market_value": account.get("total_market_value"),
                           "available_cash": account.get("cash")},
                          [{"quantity": row.get("quantity"), "available_quantity": row.get("sellable_quantity"),
                            "price": row.get("market_price"), "market_value": row.get("market_value")} for row in positions])
    return BrokerPortfolioSnapshotInput.model_validate({
        "account_key": value["account_key"], "source": value["source"], "source_snapshot_key": bundle_hash,
        "observed_at": observed, "verification": "verified_exact",
        "cash": account.get("cash"), "total_asset": account.get("total_asset"),
        "total_market_value": account.get("total_market_value"), "positions": positions, "metadata": metadata,
    })
