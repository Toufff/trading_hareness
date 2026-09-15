"""Manual broker import orchestration and audit; never controls a client."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import build_opener, ProxyHandler
import uuid
from psycopg.types.json import Json

from .automation_run_repository import start_run, finish_run, fail_run
from .broker_desktop_evidence import CONTRACT, load_manual_envelope, verify_account_binding
from .broker_fact_sync_rules import SHANGHAI
from .personal_decision_repository import persist_broker_snapshot

TASK_KEY = "broker_holdings_manual"
MAX_RUN_AGE = timedelta(minutes=15)


def validate_manual_request(phase, authorized, account_key):
    if phase != "manual":
        raise ValueError("BROKER_MANUAL_ONLY: scheduled holdings synchronization is retired")
    if not authorized:
        raise ValueError("BROKER_MANUAL_AUTHORIZATION_REQUIRED")
    if not account_key:
        raise ValueError("ACCOUNT_BINDING_REQUIRED: account_key must be selected explicitly")


def alert_path(evidence_root, run_id, code):
    folder = Path(evidence_root) / str(run_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "alert.md"
    path.write_text(f"# 手动持仓同步未完成\n\n运行：{run_id}\n\n原因：{code}\n\n未确认本次导入成功；旧快照保留原观察时间，仅按原有效期使用。\n", encoding="utf-8")
    return str(path)


def start_manual(connection, account_key, evidence_root, *, now=None):
    now = now or datetime.now(timezone.utc)
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (account_key + ":broker-sync",))
        existing = connection.execute(
            "SELECT snapshot_id FROM quant.broker_portfolio_snapshots WHERE account_key=%s ORDER BY observed_at DESC LIMIT 1",
            (account_key,),
        ).fetchone()
        if not existing:
            raise ValueError("ACCOUNT_BINDING_REQUIRED: no existing account; explicit registration is required")
        running = connection.execute(
            """SELECT * FROM quant.automation_runs WHERE task_key=%s AND status='running'
                 AND input_summary->>'account_key'=%s ORDER BY started_at DESC LIMIT 1""", (TASK_KEY, account_key),
        ).fetchone()
        if running and now - running["started_at"] <= MAX_RUN_AGE:
            return {"status": "skipped_running", "run_id": str(running["run_id"])}
        if running:
            fail_run(connection, str(running["run_id"]), ValueError("BROKER_MANUAL_RUN_EXPIRED"), error_class="BROKER_MANUAL_RUN_EXPIRED")
            alert_path(evidence_root, running["run_id"], "BROKER_MANUAL_RUN_EXPIRED")
        run_id = start_run(connection, task_key=TASK_KEY, run_key="broker-manual:" + str(uuid.uuid4()),
                           cadence="manual", as_of_date=now.astimezone(SHANGHAI).date(), methodology_version=CONTRACT,
                           input_summary={"account_key": account_key, "trigger": "manual", "ui_operations": False})
    folder = Path(evidence_root) / run_id
    folder.mkdir(parents=True, exist_ok=True)
    return {"status": "started", "run_id": run_id, "account_key": account_key, "evidence_dir": str(folder),
            "deadline_at": (now + MAX_RUN_AGE).isoformat(), "sync_mode": "manual",
            "existing_snapshot_id": str(existing["snapshot_id"]), "client_operations": False}


def read_api(base_url, account_key, *, adapter=False):
    prefix = "/api/research" if adapter else "/api/v1"
    url = base_url.rstrip("/") + prefix + "/personal/portfolio-snapshots/latest?" + urlencode({"account_key": account_key})
    with build_opener(ProxyHandler({})).open(url, timeout=15) as response:
        return json.load(response)


def confirm_manual_account(connection, run, envelope, confirmation_record, *, now=None):
    """Record a real current-user mapping confirmation in the existing run.

    Only the trusted manual CLI calls this write; ingestion APIs cannot create
    the audit by submitting an envelope flag. The caller must copy an actual
    user message, not synthesize consent. No complete account id is inferred.
    """
    now = now or datetime.now(timezone.utc)
    snapshot = load_manual_envelope(envelope, run, now=now)
    binding = snapshot.metadata["account_binding"]
    if binding.get("method") != "user_confirmed_once" or str(binding.get("confirmation_run_id")) != str(run["run_id"]):
        raise ValueError("BROKER_CONFIRMATION_RUN_MISMATCH")
    path = Path(confirmation_record)
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > 20_000:
        raise ValueError("BROKER_USER_CONFIRMATION_RECORD_REQUIRED")
    body = path.read_bytes()
    record = json.loads(body.decode("utf-8-sig"))
    from .broker_desktop_evidence import aware_time
    confirmed_at = aware_time(record.get("confirmed_at"), "confirmed_at")
    if (record.get("schema_version") != "broker-account-confirmation-v1"
            or record.get("source") != "current_user_message" or record.get("actor") != "user"
            or not isinstance(record.get("text"), str) or not 3 <= len(record["text"]) <= 2000
            or not isinstance(record.get("message_ref"), str) or not record["message_ref"].strip()):
        raise ValueError("BROKER_USER_CONFIRMATION_RECORD_REQUIRED")
    if confirmed_at < run["started_at"] or confirmed_at > now + timedelta(seconds=5):
        raise ValueError("BROKER_USER_CONFIRMATION_TIME_INVALID")
    if (record.get("account_key") != snapshot.account_key or str(record.get("run_id")) != str(run["run_id"])
            or record.get("evidence_bundle_sha256") != snapshot.metadata["evidence_bundle_sha256"]):
        raise ValueError("BROKER_CONFIRMATION_CAPTURE_MISMATCH")
    confirmation = {"source": "current_user_message", "message_ref": record["message_ref"], "text": record["text"],
                    "confirmed_at": confirmed_at.isoformat(), "recorded_at": now.isoformat(),
                    "account_key": snapshot.account_key, "existing_snapshot_id": binding["existing_snapshot_id"],
                    "account_identity": snapshot.metadata["account_identity"],
                    "evidence_bundle_sha256": snapshot.metadata["evidence_bundle_sha256"],
                    "confirmation_record": {"path": str(path.resolve()), "sha256": sha256(body).hexdigest()},
                    "trust_boundary": "explicit_user_mapping_not_full_account_identifier_verification"}
    with connection.transaction():
        locked = connection.execute("SELECT * FROM quant.automation_runs WHERE run_id=%s FOR UPDATE", (run["run_id"],)).fetchone()
        if not locked or locked["task_key"] != TASK_KEY:
            raise ValueError("BROKER_CONFIRMATION_REQUIRES_ACTIVE_MANUAL_RUN")
        summary = dict(locked.get("input_summary") or {})
        if summary.get("account_key") != snapshot.account_key:
            raise ValueError("BROKER_ACCOUNT_IDENTITY_MISMATCH")
        existing = connection.execute(
            "SELECT snapshot_id FROM quant.broker_portfolio_snapshots WHERE account_key=%s AND snapshot_id=%s",
            (snapshot.account_key, binding["existing_snapshot_id"]),
        ).fetchone()
        if not existing:
            raise ValueError("ACCOUNT_BINDING_REQUIRED")
        prior = summary.get("account_confirmation")
        if prior:
            comparable = lambda value: {key: item for key, item in value.items() if key != "recorded_at"}
            if comparable(prior) != comparable(confirmation):
                raise ValueError("BROKER_CONFIRMATION_IMMUTABLE_CONFLICT")
            return {"status": "account_confirmation_idempotent", "confirmation_run_id": str(run["run_id"])}
        if locked["status"] != "running":
            raise ValueError("BROKER_CONFIRMATION_REQUIRES_ACTIVE_MANUAL_RUN")
        summary["account_confirmation"] = confirmation
        connection.execute("UPDATE quant.automation_runs SET input_summary=%s,updated_at=now() WHERE run_id=%s",
                           (Json(summary), run["run_id"]))
    return {"status": "account_confirmed", "confirmation_run_id": str(run["run_id"]),
            "account_key": snapshot.account_key, "evidence_bundle_sha256": snapshot.metadata["evidence_bundle_sha256"],
            "identity_verified_by": "user_confirmation", "full_account_identifier_verified": False}


def verify_readback(snapshot, saved, readback):
    if str(readback.get("snapshot_id")) != str(saved["snapshot_id"]):
        raise ValueError("BROKER_API_READBACK_MISMATCH")
    for field in ("account_key", "source", "source_snapshot_key", "verification"):
        if readback.get(field) != getattr(snapshot, field):
            raise ValueError("BROKER_API_READBACK_MISMATCH: " + field)
    stamp = datetime.fromisoformat(str(readback.get("observed_at", "")).replace("Z", "+00:00"))
    if stamp != snapshot.observed_at:
        raise ValueError("BROKER_API_OBSERVATION_TIME_MISMATCH")
    for field in ("cash", "total_asset", "total_market_value"):
        if Decimal(str(readback.get(field))) != getattr(snapshot, field):
            raise ValueError("BROKER_API_ACCOUNT_MISMATCH: " + field)
    got = readback.get("positions") or []
    mapping = {row["symbol"]: row for row in got}
    if len(got) != len(snapshot.positions) or len(mapping) != len(got):
        raise ValueError("BROKER_API_POSITION_COUNT_MISMATCH")
    for position in snapshot.positions:
        row = mapping.get(position.symbol) or {}
        if row.get("name") != position.name:
            raise ValueError("BROKER_API_POSITION_MISMATCH")
        for field in ("quantity", "sellable_quantity", "average_cost", "market_price", "market_value", "unrealized_pnl"):
            expected, actual = getattr(position, field), row.get(field)
            if (expected is None) != (actual is None) or (expected is not None and Decimal(str(actual)) != expected):
                raise ValueError("BROKER_API_POSITION_MISMATCH: " + field)
    metadata = readback.get("metadata") or {}
    for field in ("trade_date", "account_identity", "evidence_bundle_sha256"):
        if metadata.get(field) != snapshot.metadata[field]:
            raise ValueError("BROKER_API_EVIDENCE_MISMATCH: " + field)


def complete_manual(connection, run, envelope, evidence_root, owner_url, adapter_url, *, validate_only=False):
    snapshot = load_manual_envelope(envelope, run)
    verify_account_binding(connection, snapshot.account_key, snapshot.metadata)
    folder = Path(evidence_root) / str(run["run_id"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "validated-snapshot.json").write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    if validate_only:
        return {"status": "validated", "observed_at": snapshot.observed_at.isoformat(),
                "trade_date": snapshot.metadata["trade_date"], "position_count": len(snapshot.positions)}
    with connection.transaction():
        saved = persist_broker_snapshot(connection, snapshot)
    row = connection.execute(
        """SELECT snapshot_id,account_key,source,source_snapshot_key,observed_at,verification,
                  cash,total_asset,total_market_value,content_hash,metadata
             FROM quant.broker_portfolio_snapshots WHERE snapshot_id=%s""", (saved["snapshot_id"],),
    ).fetchone()
    if not row or row["content_hash"] != saved["content_hash"]:
        raise ValueError("BROKER_DATABASE_READBACK_MISMATCH")
    positions = connection.execute(
        """SELECT symbol,name,quantity,sellable_quantity,average_cost,market_price,market_value,unrealized_pnl
             FROM quant.broker_position_snapshots WHERE snapshot_id=%s ORDER BY symbol""", (saved["snapshot_id"],),
    ).fetchall()
    database_readback = {**dict(row), "observed_at": row["observed_at"].isoformat(),
                         "positions": [dict(item) for item in positions]}
    verify_readback(snapshot, saved, database_readback)
    owner = read_api(owner_url, snapshot.account_key)
    adapter = read_api(adapter_url, snapshot.account_key, adapter=True)
    if owner.get("snapshot_id") != adapter.get("snapshot_id") or owner.get("content_hash") != adapter.get("content_hash"):
        raise ValueError("BROKER_OWNER_ADAPTER_MISMATCH")
    historical = str(owner.get("snapshot_id")) != str(saved["snapshot_id"])
    if historical:
        if datetime.fromisoformat(owner["observed_at"].replace("Z", "+00:00")) <= snapshot.observed_at:
            raise ValueError("BROKER_API_READBACK_MISMATCH")
    else:
        verify_readback(snapshot, saved, owner)
        verify_readback(snapshot, saved, adapter)
    summary = {"status": "verified_exact", "snapshot_id": str(saved["snapshot_id"]),
               "observed_at": snapshot.observed_at.isoformat(), "trade_date": snapshot.metadata["trade_date"],
               "position_count": len(snapshot.positions), "account_key": snapshot.account_key,
               "source": snapshot.source, "historical_import": historical, "subsequent_trades": "unknown",
               "owner_adapter_verified": True, "database_rows_verified": True,
               "run_id": str(run["run_id"]), "evidence_dir": str(folder)}
    with connection.transaction():
        finish_run(connection, str(run["run_id"]), output_summary=summary)
    (folder / "receipt.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
