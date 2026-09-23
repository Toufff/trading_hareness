"""Bounded two-stage strict-OHLC enrichment for advanced scan candidates."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any, Callable

from ..longhu_vendor_source import LonghuVendorSource
from ..stock_workbench_service import _fetch_history, parse_longhu_history


def fetch(
    symbols: list[str], day: date, *, lookback_days: int = 80, workers: int = 8,
    source_factory: Callable[[], Any] = LonghuVendorSource,
) -> tuple[dict[str, list[dict]], dict]:
    unique = list(dict.fromkeys(symbols))[:128]
    histories: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    strict_ready = 0

    def one(symbol: str) -> tuple[str, list[dict], dict]:
        envelope = _fetch_history(source_factory, symbol, lookback_days)
        rows, health = parse_longhu_history(envelope)
        rows = [row for row in rows if row["date"] <= str(day)]
        return symbol, rows, health

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 12))) as pool:
        pending = {pool.submit(one, symbol): symbol for symbol in unique}
        for future in as_completed(pending):
            symbol = pending[future]
            try:
                key, rows, health = future.result()
                if len(rows) >= 40 and health.get("status") in {"ready", "partial"}:
                    histories[key] = rows
                    strict_ready += 1
                else:
                    if rows and health.get("status") in {"ready", "partial"}:
                        # A recent listing may have valid daily OHLC, but not
                        # enough bars for an advanced 40-bar setup.
                        histories[key] = rows
                    errors[symbol] = f"strict_ohlc_rows={len(rows)}"
            except Exception as exc:  # retain per-symbol diagnostics
                errors[symbol] = f"{type(exc).__name__}: {exc}"
    return histories, {
        "requested": len(unique), "ready": strict_ready, "failed": len(errors),
        "daily_ready": len(histories),
        "errors": dict(list(errors.items())[:20]), "source": "longhuvip:GetKLineDay_W14",
        "lookback_days": lookback_days, "workers": max(1, min(workers, 12)),
        "note": "只对收盘快照预筛选出的有限候选补充严格OHLC；不是5000只逐票暴力抓取。",
    }


__all__ = ["fetch"]
