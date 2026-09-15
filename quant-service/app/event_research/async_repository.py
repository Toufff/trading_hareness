"""Native async read for the dashboard; never fetch providers on GET."""
from datetime import datetime, timezone
from .contracts import timestamp
from .schedule import delivery_state

async def latest(async_database):
    now=datetime.now(timezone.utc)
    async with async_database.transaction() as c:
        cursor=await c.execute('''SELECT result FROM quant.event_research_runs
            WHERE cutoff<=%s ORDER BY cutoff DESC,created_at DESC LIMIT 1''',(now,))
        row=await cursor.fetchone()
    if not row:return dict(status='absent',events=[],leads=[],summary='本时点没有可用消息快照。')
    value=row['result'];age=(now-timestamp(value['cutoff'])).total_seconds()
    return {**value,**delivery_state(value,now),'age_seconds':round(age)}
