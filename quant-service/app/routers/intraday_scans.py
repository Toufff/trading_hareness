"""Read-only same-run intraday research API, no scan side effects."""
from uuid import UUID
from fastapi import APIRouter,Query
from ..intraday_scan.read_service import latest

def build_intraday_scans_router(database,run_database):
    router=APIRouter(tags=['intraday-scans'])
    @router.get('/api/v1/intraday-scans')
    async def intraday_scans(run_id:UUID|None=None,lane:str|None=Query(default=None,max_length=30),
                             symbol:str|None=Query(default=None,pattern=r'^\d{6}\.(SH|SZ)$')):
        return await run_database(latest,database,str(run_id) if run_id else None,lane,symbol,timeout_seconds=20)
    return router
