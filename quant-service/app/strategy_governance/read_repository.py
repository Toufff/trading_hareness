"""Native async bounded projections; browser exposes no approval endpoint."""
from ..database import AsyncDatabase

NOTICE = '独立审查与影子实验不改变正式策略；待落地须人工在本机批准。通过工程验收不代表盈利有效。'


async def dashboard(async_database: AsyncDatabase, limit=100):
    limit = max(1, min(200, limit))
    async with async_database.transaction() as c:
        cursor = await c.execute('SELECT snapshot FROM quant.strategy_governance_items ORDER BY updated_at DESC LIMIT %s', (limit,))
        rows = await cursor.fetchall()
        cursor = await c.execute('SELECT state,count(*) AS count FROM quant.strategy_governance_items GROUP BY state')
        counts = {r['state']:r['count'] for r in await cursor.fetchall()}
        cursor = await c.execute('SELECT generation,item_id,artifact_hash,action,recorded_at FROM quant.strategy_governance_activations ORDER BY generation DESC LIMIT 1')
        active = await cursor.fetchone()
        ids=[r['snapshot']['id'] for r in rows]
        cursor=await c.execute('''SELECT DISTINCT ON (item_id) item_id,evidence,recorded_at
            FROM quant.strategy_governance_diagnostics WHERE item_id=ANY(%s)
            ORDER BY item_id,recorded_at DESC''',(ids,))
        diagnostics={r['item_id']:{**r['evidence'],'recorded_at':r['recorded_at']} for r in await cursor.fetchall()}
    return {'status':'ok', 'items':[{**r['snapshot'],'latest_diagnostic':diagnostics.get(r['snapshot']['id'])} for r in rows], 'counts':counts, 'notice':NOTICE,
            'active':dict(active) if active else None, 'truncated':sum(counts.values()) > len(rows)}
