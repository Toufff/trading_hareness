"""Centralized environment-variable configuration for the composition root.

Every field here mirrors one ``os.getenv`` call that used to live inline in
``app/main.py``.  ``Settings.from_environ()`` does a fresh parse of the given
(or process) environment on every call -- it does not cache -- so call sites
that need a live re-read after the environment changes (tests, and the
one-shot startup checks in ``application_lifecycle``) keep working exactly as
before.  Call sites that only ever need the value that was true at process
start can still call it once and hold the result.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


def _flag(value: str | None, *, default: bool) -> bool:
    """Parse the shared boolean vocabulary this service accepts everywhere."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _clamped_int(value: str | None, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except ValueError:
        parsed = default
    return max(minimum, min(maximum, parsed))


@dataclass(frozen=True)
class Settings:
    quant_universe: tuple[str, ...]
    dashboard_public_url: str | None
    shared_read_api_key: str
    write_api_key: str
    allow_unauthenticated_writes: bool
    data_dir: str
    legacy_schema_bootstrap_enabled: bool
    control_plane_writes_enabled: bool
    provider_global_rate_limit_max_wait_seconds: float

    intraday_minute_profile_capture_enabled: bool
    intraday_minute_profile_retention_days: int
    intraday_minute_profile_max_symbols: int
    longhu_intraday_max_symbols: int

    strategy_review_automation_enabled: bool
    post_close_strategy_automation_enabled: bool
    ten_day_leader_rotation_automation_enabled: bool
    daily_summary_automation_enabled: bool

    ths_concept_member_backfill_enabled: bool
    ths_concept_member_backfill_batch_size: int
    all_board_member_backfill_enabled: bool
    all_board_member_backfill_batch_size: int

    retention_maintenance_automation_enabled: bool

    market_event_capture_enabled: bool
    all_a_level1_capture_enabled: bool

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        dashboard_public_url = (env.get("QUANT_DASHBOARD_PUBLIC_URL") or "").strip().rstrip("/") or None
        quant_universe = tuple(item.strip() for item in (env.get("QUANT_UNIVERSE") or "").split(",") if item.strip())
        try:
            rate_limit_wait = min(30.0, max(0.0, float(env.get("QUANT_PROVIDER_GLOBAL_RATE_LIMIT_MAX_WAIT_SECONDS", "5"))))
        except (TypeError, ValueError):
            rate_limit_wait = 5.0
        return cls(
            quant_universe=quant_universe,
            dashboard_public_url=dashboard_public_url,
            shared_read_api_key=env.get("QUANT_SHARED_READ_API_KEY", ""),
            write_api_key=env.get("QUANT_WRITE_API_KEY", "").strip(),
            allow_unauthenticated_writes=_flag(env.get("QUANT_ALLOW_UNAUTHENTICATED_WRITES"), default=False),
            data_dir=env.get("QUANT_DATA_DIR", "/var/lib/quant"),
            legacy_schema_bootstrap_enabled=_flag(env.get("QUANT_LEGACY_SCHEMA_BOOTSTRAP"), default=False),
            control_plane_writes_enabled=_flag(env.get("QUANT_CONTROL_PLANE_WRITES_ENABLED"), default=True),
            provider_global_rate_limit_max_wait_seconds=rate_limit_wait,
            intraday_minute_profile_capture_enabled=_flag(env.get("INTRADAY_MINUTE_PROFILE_CAPTURE_ENABLED"), default=True),
            intraday_minute_profile_retention_days=_clamped_int(
                env.get("INTRADAY_MINUTE_PROFILE_RETENTION_DAYS"), default=90, minimum=20, maximum=365),
            intraday_minute_profile_max_symbols=_clamped_int(
                env.get("INTRADAY_MINUTE_PROFILE_MAX_SYMBOLS"), default=40, minimum=1, maximum=40),
            longhu_intraday_max_symbols=_clamped_int(
                env.get("QUANT_LONGHU_INTRADAY_MAX_SYMBOLS"), default=24, minimum=1, maximum=60),
            strategy_review_automation_enabled=_flag(env.get("STRATEGY_REVIEW_AUTOMATION_ENABLED"), default=True),
            post_close_strategy_automation_enabled=_flag(env.get("POST_CLOSE_STRATEGY_AUTOMATION_ENABLED"), default=True),
            ten_day_leader_rotation_automation_enabled=_flag(
                env.get("TEN_DAY_LEADER_ROTATION_AUTOMATION_ENABLED"), default=True),
            daily_summary_automation_enabled=_flag(env.get("DAILY_SUMMARY_AUTOMATION_ENABLED"), default=True),
            ths_concept_member_backfill_enabled=_flag(env.get("THS_CONCEPT_MEMBER_BACKFILL_ENABLED"), default=True),
            ths_concept_member_backfill_batch_size=_clamped_int(
                env.get("THS_CONCEPT_MEMBER_BACKFILL_BATCH_SIZE"), default=25, minimum=1, maximum=25),
            all_board_member_backfill_enabled=_flag(env.get("ALL_BOARD_MEMBER_BACKFILL_ENABLED"), default=True),
            all_board_member_backfill_batch_size=_clamped_int(
                env.get("ALL_BOARD_MEMBER_BACKFILL_BATCH_SIZE"), default=10, minimum=1, maximum=25),
            retention_maintenance_automation_enabled=_flag(
                env.get("QUANT_RETENTION_MAINTENANCE_AUTOMATION_ENABLED"), default=False),
            market_event_capture_enabled=_flag(env.get("MARKET_EVENT_CAPTURE_ENABLED"), default=True),
            all_a_level1_capture_enabled=_flag(env.get("ALL_A_LEVEL1_CAPTURE_ENABLED"), default=True),
        )


__all__ = ["Settings"]
