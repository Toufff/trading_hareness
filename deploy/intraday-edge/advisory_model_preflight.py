"""Synthetic, secret-free live preflight for advisory model transports."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.intraday_advisory.model import CodexAdvisoryModel, DeepSeekAdvisoryModel

PAYLOAD = {
    "as_of": "2026-09-21T10:00:00+08:00", "trigger_kind": "preflight", "report_kind": "fixed",
    "research_only": True, "live_orders": False, "scope_blockers": [], "recent_events": [],
    "scope": [{"symbol": "600000.SH", "name": "合成测试标的", "scope": "recommendation",
               "position_or_recommendation": {"reason": "transport_preflight"},
               "quote": {"price": 10.10, "pre_close": 10.00, "pct_change": 1.0,
                         "cumulative_amount": 10000000, "observed_at": "2026-09-21T10:00:00+08:00"}}],
}


async def main(provider: str) -> int:
    model = DeepSeekAdvisoryModel() if provider == "deepseek" else CodexAdvisoryModel()
    result = await model.analyze(PAYLOAD)
    required = {"market_state", "summary", "attention_symbols", "guidance", "risks", "state_fingerprint"}
    missing = sorted(required - set(result.output))
    if missing:
        raise ValueError("advisory model response missing fields: " + ",".join(missing))
    print(json.dumps({"status": "passed", "provider": provider, "model": result.model,
                      "duration_ms": result.duration_ms, "market_state": result.output["market_state"],
                      "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("deepseek", "codex"), required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args().provider)))
