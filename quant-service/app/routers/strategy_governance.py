"""Read-only governance inspection; mutations are deliberately absent from HTTP."""
from fastapi import APIRouter, Query
from ..strategy_governance.read_repository import dashboard


def build_strategy_governance_router(async_database):
    router = APIRouter(tags=['strategy-governance'])

    @router.get('/api/v1/strategy/governance')
    async def governance(limit: int = Query(100, ge=1, le=200)) -> dict:
        return await dashboard(async_database, limit)

    return router
