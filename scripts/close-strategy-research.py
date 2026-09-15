"""Execute account-independent decision closure for one persisted lane scan."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "quant-service"))

from app.database import Database
from app.decision_research_service import refresh_new_buy_research_and_plans


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--candidate-limit", type=int, default=12)
    args = parser.parse_args()
    result = refresh_new_buy_research_and_plans(
        Database(), args.date, candidate_limit=max(1, min(args.candidate_limit, 30)),
    )
    print(json.dumps(result, ensure_ascii=False, default=str))
    complete = (
        result.get("status") == "completed"
        and result.get("incomplete_candidates", 0) == 0
    )
    raise SystemExit(0 if complete else 2)


if __name__ == "__main__":
    main()
