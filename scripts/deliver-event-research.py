"""Silent scheduler entry; manual verification never completes a future slot."""
import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'quant-service'))
from dotenv import load_dotenv
from app.database import Database
from app.event_research.delivery import deliver


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', default='G:/StockPlatform/config/runtime.env')
    parser.add_argument('--platform-root', type=Path, default=Path('G:/StockPlatform'))
    parser.add_argument('--api-base', default='http://127.0.0.1:5681')
    parser.add_argument('--manual', action='store_true')
    args = parser.parse_args()
    load_dotenv(args.env_file, override=True)
    db = Database()
    code = 0
    try:
        result = deliver(db, output_dir=args.platform_root / 'reports/events', api_base=args.api_base, manual=args.manual)
    except Exception as error:
        # No response bodies, environment or credentials in scheduler logs.
        result = dict(status='failed', error_type=type(error).__name__)
        code = 2
    finally:
        db.close()
    result.update(recorded_at=datetime.now(timezone.utc).isoformat(), manual=args.manual)
    log = args.platform_root / 'logs/event-research-delivery.jsonl'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(result, ensure_ascii=False) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
