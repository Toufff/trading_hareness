"""Bounded persistence seam for frozen intraday rule inputs."""

from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

from psycopg.types.json import Json

from .intraday_rule_inputs import intraday_rule_input_hash, intraday_rule_input_payload


def persist_rule_input_snapshot(connection: Any, *, scan_id: uuid.UUID, observed_at: datetime,
                                watch: dict[str, Any], quote: dict[str, Any] | None,
                                previous_quote: dict[str, Any] | None,
                                daily_factors: dict[str, Any] | None,
                                minute_features: dict[str, Any] | None,
                                peer_context: dict[str, Any] | None,
                                model_version: str) -> str:
    """Store one minimal replay input for every scanned watch, even no-signal rows."""
    payload = intraday_rule_input_payload(
        watch=watch, quote=quote, previous_quote=previous_quote, daily_factors=daily_factors,
        minute_features=minute_features, peer_context=peer_context, model_version=model_version,
    )
    input_hash = intraday_rule_input_hash(payload)
    connection.execute(
        """INSERT INTO quant.intraday_rule_input_snapshots(
               scan_id,symbol,observed_at,model_version,input_hash,inputs
           ) VALUES(%s,%s,%s,%s,%s,%s)
           ON CONFLICT(scan_id,symbol,model_version) DO NOTHING""",
        (scan_id, str(watch["symbol"]), observed_at, model_version, input_hash, Json(payload)),
    )
    return input_hash


def prune_rule_input_evidence(connection: Any, *, cutoff: datetime) -> None:
    """Bound future replay evidence without touching alerts, outcomes or daily data."""
    connection.execute(
        "DELETE FROM quant.intraday_rule_input_snapshots WHERE observed_at<%s",
        (cutoff,),
    )
    # The associated watch-price evidence is the only non-raw quote source
    # required to audit the frozen input snapshots.  Fast/order-book streams
    # have their stricter independent retention because they are attribution
    # evidence rather than core-rule inputs.
    connection.execute(
        "DELETE FROM quant.intraday_quote_observations WHERE source_name IN ('tencent_free','sina_free') AND observed_at<%s",
        (cutoff,),
    )


__all__ = ["persist_rule_input_snapshot", "prune_rule_input_evidence"]
