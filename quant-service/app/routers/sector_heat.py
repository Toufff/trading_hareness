"""One read-only sector heat projection for both local and public dashboards."""
from datetime import datetime
from fastapi import APIRouter, Query
from ..sector_heat.context import build_context
from ..sector_heat.repository import SHANGHAI


def _read_heat(database):
    with database.transaction() as connection:
        return build_context(connection,phase='close',as_of=datetime.now(SHANGHAI).isoformat())


def build_sector_heat_router(database, run_database):
    router=APIRouter(tags=['sector-heat'])

    @router.get('/api/v1/sector-heat')
    async def sector_heat(key: str | None=Query(default=None,max_length=160)):
        heat=await run_database(_read_heat,database,timeout_seconds=20)
        heat['source']='G盘 PostgreSQL：Longhu 已存观测；行业新序列为成员等权涨幅/合计资金，非官方板块指数'
        heat['warnings'].append('迁移前后指标口径不同的历史不拼接；概念没有当天数据时不以旧数据补齐。')
        if key:
            heat['items']=[item for item in heat['items'] if item['key']==key]
        else:
            heat['items']=[{k:v for k,v in item.items() if k not in ('daily_history','intraday_history','factors')} for item in heat['items']]
        return {'sector_heat':heat}

    return router
