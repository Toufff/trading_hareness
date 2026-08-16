"""Bounded public-market snapshot persistence and orchestration.

The live service passes its database/executor functions in explicitly.  This
keeps the provider-facing work out of ``main.py`` while retaining the existing
FastAPI compatibility wrappers and their test seams.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from .akshare_provider import AkShareProviderError, akshare_tencent_all_a_spot
from .free_market_providers import sina_quotes
from .market_flow_repository import persist_market_snapshot_flow_feature
from .market_snapshots import snapshot_status, summarize_quotes
from .provider_health import record_provider_failure, record_provider_success
from .public_market_repository import persist_free_quotes
from .runtime_executors import ExecutorSaturatedError
from .tushare_providers import safe_error_detail


class MarketSnapshotActions:
    """Own the persistence side of a coverage-gated market snapshot."""

    def __init__(self, database: Any) -> None:
        self._database = database

    @staticmethod
    def thresholds() -> tuple[int, float, set[str]]:
        minimum_universe = max(100, int(os.getenv("MARKET_SNAPSHOT_MIN_UNIVERSE", "1000")))
        coverage = min(1.0, max(0.1, float(os.getenv("MARKET_SNAPSHOT_MIN_COVERAGE", "0.95"))))
        licensed = {
            item.strip()
            for item in os.getenv("MARKET_SNAPSHOT_LICENSED_PROVIDERS", "").split(",")
            if item.strip()
        }
        return minimum_universe, coverage, licensed

    @staticmethod
    def public_quote_settings() -> dict[str, int | bool]:
        """Read bounded public-quote settings without turning an invalid env into load."""
        enabled = os.getenv("MARKET_SNAPSHOT_ENABLE_PUBLIC_BATCH", "false").strip().lower() in {"1", "true", "yes", "on"}
        try:
            batch_size = int(os.getenv("MARKET_SNAPSHOT_PUBLIC_BATCH_SIZE", "80"))
        except ValueError:
            batch_size = 80
        try:
            concurrency = int(os.getenv("MARKET_SNAPSHOT_PUBLIC_CONCURRENCY", "2"))
        except ValueError:
            concurrency = 2
        return {
            "enabled": enabled,
            "batch_size": min(200, max(1, batch_size)),
            "concurrency": min(8, max(1, concurrency)),
        }

    @staticmethod
    def tencent_enabled() -> bool:
        """Use one Tencent all-A snapshot instead of dozens of public batches."""
        return os.getenv("MARKET_SNAPSHOT_ENABLE_TENCENT_SNAPSHOT", "true").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def tencent_quotes(
        rows: list[dict[str, Any]],
        exchange_date: date,
        quote_mapper: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> list[dict[str, Any]]:
        """Normalize the already-used Tencent all-A cross-section for storage."""
        result: list[dict[str, Any]] = []
        for row in rows:
            quote = quote_mapper(row)
            if not quote:
                continue
            result.append({
                "ts_code": quote["symbol"], "name": quote.get("name"), "close": quote.get("price"),
                "pct_chg": quote.get("pct_change"), "vol": row.get("vol") or row.get("volume"),
                "amount": quote.get("turnover"), "volume_ratio": quote.get("volume_ratio"),
                "turnover_rate": quote.get("turnover_rate"), "main_net_inflow": quote.get("main_net_inflow"),
                "trade_date": exchange_date.strftime("%Y%m%d"), "source_session_date_inferred": True,
            })
        return result

    @staticmethod
    def quote_is_for_exchange_date(quote: dict[str, Any], exchange_date: date) -> bool:
        raw_date = str(quote.get("trade_date") or "").replace("-", "")
        return raw_date == exchange_date.strftime("%Y%m%d")

    def universe_symbols(self, universe_key: str) -> list[str]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT symbol FROM quant.universe_members WHERE universe_key=%s AND enabled ORDER BY symbol",
                (universe_key,),
            ).fetchall()
        return [str(row["symbol"]) for row in rows]

    def persist_public_quote_batch(self, provider: str, quotes: list[dict[str, Any]], latency_ms: int | None = None) -> int:
        """Persist one bounded public quote batch and its health outcome off-loop."""
        stored = persist_free_quotes(provider, quotes)
        with self._database.transaction() as connection:
            record_provider_success(connection, provider, "realtime_quote", stored, latency_ms)
        return stored

    def persist_public_quote_failure(self, provider: str, detail: str, latency_ms: int | None = None) -> None:
        with self._database.transaction() as connection:
            record_provider_failure(connection, provider, "realtime_quote", detail, latency_ms)

    def finalize(
        self,
        request: Any,
        observed_at: datetime,
        exchange_date: date,
        symbols: list[str],
        minimum_universe: int,
        minimum_coverage: float,
        licensed_providers: set[str],
        public_quote_settings: dict[str, Any],
        planned_public_requests: int,
        refresh_error: str | None,
        refresh_skipped: str | None,
        tencent_status: dict[str, Any],
    ) -> dict[str, Any]:
        """Read fresh evidence and write the idempotent snapshot in one DB worker."""
        fresh_after = observed_at - timedelta(minutes=10)
        with self._database.transaction() as connection:
            quote_rows = connection.execute(
                """WITH active AS (
                       SELECT symbol FROM quant.universe_members WHERE universe_key=%s AND enabled
                     ), latest AS (
                       SELECT DISTINCT ON (o.symbol) o.symbol,o.provider_key,o.available_at,o.normalized
                       FROM quant.raw_market_observations o JOIN active a ON a.symbol=o.symbol
                       WHERE o.capability='realtime_quote' AND o.available_at>=%s
                       ORDER BY o.symbol,o.available_at DESC
                     ) SELECT symbol,provider_key,available_at,normalized FROM latest""",
                (request.universe_key, fresh_after),
            ).fetchall()
            dated_rows = [
                row for row in quote_rows
                if isinstance(row["normalized"], dict)
                and self.quote_is_for_exchange_date(dict(row["normalized"]), exchange_date)
            ]
            quotes = [dict(row["normalized"]) for row in dated_rows]
            providers = {str(row["provider_key"]) for row in dated_rows}
            provider_counts = {
                provider: sum(str(row["provider_key"]) == provider for row in dated_rows)
                for provider in sorted(providers)
            }
            status, decision_eligible, flags = snapshot_status(
                universe_count=len(symbols), quote_count=len(quotes), minimum_universe=minimum_universe,
                minimum_coverage=minimum_coverage, licensed_providers=licensed_providers, observed_providers=providers,
            )
            stale_quote_dates = len(quote_rows) - len(dated_rows)
            if stale_quote_dates:
                flags.append("quote_trade_date_mismatch")
            if refresh_error:
                flags.append("public_quote_refresh_failed")
            if refresh_skipped:
                flags.append(refresh_skipped)
            coverage = len(quotes) / len(symbols) if symbols else 0.0
            summary = summarize_quotes(quotes)
            source_summary = {
                "providers": provider_counts,
                "fresh_after": fresh_after.isoformat(),
                "stale_quote_dates": stale_quote_dates,
                "refresh_error": refresh_error,
                "refresh_skipped": refresh_skipped,
                "tencent_snapshot": tencent_status,
                "licensed_providers": sorted(licensed_providers),
                "public_quotes_are_supplemental": True,
                "public_quote_batch": {**public_quote_settings, "planned_requests": planned_public_requests},
            }
            snapshot_key = hashlib.sha256(f"{exchange_date}:{request.session}:{request.universe_key}".encode()).hexdigest()
            connection.execute(
                """INSERT INTO quant.market_snapshot_runs(snapshot_key,session,exchange_date,observed_at,universe_key,universe_count,quote_count,coverage,status,
                          decision_eligible,source_summary,summary,quality_flags)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(snapshot_key) DO UPDATE SET observed_at=EXCLUDED.observed_at,universe_count=EXCLUDED.universe_count,
                     quote_count=EXCLUDED.quote_count,coverage=EXCLUDED.coverage,status=EXCLUDED.status,decision_eligible=EXCLUDED.decision_eligible,
                     source_summary=EXCLUDED.source_summary,summary=EXCLUDED.summary,quality_flags=EXCLUDED.quality_flags,updated_at=now()""",
                (snapshot_key, request.session, exchange_date, observed_at, request.universe_key, len(symbols), len(quotes),
                 Decimal(str(round(coverage, 6))), status, decision_eligible, Json(source_summary), Json(summary), Json(sorted(set(flags)))),
            )
            market_flow_feature = persist_market_snapshot_flow_feature(
                connection, session=request.session, exchange_date=exchange_date,
                observed_at=observed_at, summary=summary,
            )
        return {
            "status": status, "session": request.session, "exchange_date": str(exchange_date), "observed_at": observed_at,
            "universe_key": request.universe_key, "universe_count": len(symbols), "quote_count": len(quotes),
            "coverage": round(coverage, 6), "decision_eligible": decision_eligible, "summary": summary,
            "source_summary": source_summary, "quality_flags": sorted(set(flags)),
            "market_flow_feature": market_flow_feature,
        }

    async def build(
        self,
        request: Any,
        *,
        run_database: Callable[..., Awaitable[Any]],
        run_akshare: Callable[..., Awaitable[Any]],
        provider_capabilities: Callable[[str, list[str]], Awaitable[set[str]]],
        quote_mapper: Callable[[dict[str, Any]], dict[str, Any] | None],
        thresholds: Callable[[], tuple[int, float, set[str]]],
        public_quote_settings: Callable[[], dict[str, int | bool]],
        tencent_enabled: Callable[[], bool],
        universe_symbols: Callable[[str], list[str]],
        persist_batch: Callable[[str, list[dict[str, Any]], int | None], int],
        persist_failure: Callable[[str, str, int | None], None],
        finalize: Callable[..., dict[str, Any]],
    ) -> dict[str, Any]:
        """Create a source-labelled midday or close market snapshot.

        Public quotes are collected only as a bounded supplement.  They never
        unlock recommendation decisions without a configured licensed feed.
        """
        observed_at = datetime.now(timezone.utc)
        exchange_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        minimum_universe, minimum_coverage, licensed_providers = thresholds()
        settings = public_quote_settings()
        symbols = await run_database(universe_symbols, request.universe_key)
        refresh_error = None
        refresh_skipped = None
        tencent_status: dict[str, Any] = {"enabled": tencent_enabled(), "status": "not_attempted"}
        planned_public_requests = math.ceil(len(symbols) / int(settings["batch_size"])) if symbols else 0
        tencent_circuit_open = False
        sina_circuit_open = False
        if request.refresh_public_quotes and len(symbols) >= minimum_universe:
            tencent_circuit_open = "realtime_quote" in await provider_capabilities("tencent_free", ["realtime_quote"])
            if settings["enabled"]:
                sina_circuit_open = "realtime_quote" in await provider_capabilities("sina_free", ["realtime_quote"])
        if request.refresh_public_quotes and tencent_enabled() and len(symbols) >= minimum_universe and tencent_circuit_open:
            tencent_status = {"enabled": True, "status": "circuit_open", "notice": "provider health circuit is open; upstream request skipped"}
        elif request.refresh_public_quotes and tencent_enabled() and len(symbols) >= minimum_universe:
            try:
                started_at = asyncio.get_running_loop().time()
                raw_tencent_rows = await run_akshare(akshare_tencent_all_a_spot, timeout_seconds=25)
                stored = await run_database(
                    persist_batch, "tencent_free", self.tencent_quotes(raw_tencent_rows, exchange_date, quote_mapper),
                    round((asyncio.get_running_loop().time() - started_at) * 1000), timeout_seconds=60,
                )
                tencent_status = {
                    "enabled": True, "status": "completed", "upstream_rows": len(raw_tencent_rows), "stored": stored,
                    "session_date_inferred": True,
                }
            except ExecutorSaturatedError as error:
                detail = safe_error_detail(str(error), 500)
                # Local queue pressure says nothing about Tencent availability.
                # Keep the provider circuit untouched and allow Sina below.
                tencent_status = {"enabled": True, "status": "local_capacity", "error": detail}
            except (asyncio.TimeoutError, AkShareProviderError, ValueError) as error:
                detail = safe_error_detail(str(error), 500)
                tencent_status = {"enabled": True, "status": "failed", "error": detail}
                latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
                await run_database(persist_failure, "tencent_free", detail, latency_ms)
        elif request.refresh_public_quotes and not tencent_enabled() and not settings["enabled"]:
            refresh_skipped = "public_quote_batch_disabled"
        elif request.refresh_public_quotes and len(symbols) < minimum_universe:
            refresh_skipped = "universe_below_minimum"
        elif request.refresh_public_quotes and tencent_status["status"] != "completed" and settings["enabled"] and sina_circuit_open:
            refresh_skipped = "sina_realtime_quote_circuit_open"
        elif request.refresh_public_quotes and tencent_status["status"] != "completed" and settings["enabled"]:
            try:
                started_at = asyncio.get_running_loop().time()
                fetched = await sina_quotes(
                    symbols, batch_size=int(settings["batch_size"]), concurrency=int(settings["concurrency"]),
                )
                await run_database(
                    persist_batch, "sina_free", fetched,
                    round((asyncio.get_running_loop().time() - started_at) * 1000), timeout_seconds=60,
                )
            except Exception as error:  # noqa: BLE001
                refresh_error = safe_error_detail(str(error), 500)
                latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
                await run_database(persist_failure, "sina_free", refresh_error, latency_ms)
        return await run_database(
            finalize, request, observed_at, exchange_date, symbols, minimum_universe, minimum_coverage,
            licensed_providers, settings, planned_public_requests, refresh_error, refresh_skipped, tencent_status,
            timeout_seconds=60,
        )
