"""Guard both query families against a later legacy result hiding nine lanes."""
from pathlib import Path
import re


def test_sync_and_async_latest_share_semantic_type_priority():
    root=Path(__file__).resolve().parents[1]/'app'
    for filename in ('strategy_read_model.py','async_strategy_read_repository.py'):
        source=(root/filename).read_text(encoding='utf-8')
        body=source.split('def latest_post_close_strategy',1)[1]
        orders=re.findall(r'ORDER BY as_of_date DESC[^"\n]+',body)
        assert len(orders)==2
        assert all("(summary ? 'strategy_lanes') DESC,updated_at DESC" in x for x in orders)
    # This is a query-contract guard. Real PostgreSQL and public-page acceptance
    # separately verifies the actual later-base / earlier-nine-lane incident.
