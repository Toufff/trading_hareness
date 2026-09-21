"""Read exact persisted details on demand, pinned to a run (never latest drift)."""
from typing import Any

SECTIONS = {'report', 'followup', 'effectiveness', 'research', 'events'}


def detail_query(section: str, run_id: str, key: str | None, offset: int, limit: int):
    if section not in SECTIONS:
        raise ValueError('unknown detail section')
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    if section == 'report':
        return ("""SELECT r AS detail FROM quant.post_close_strategy_runs,
            LATERAL jsonb_array_elements(summary#>'{strategy_lanes,report_bundle,reports}') r
            WHERE run_id=%s::uuid AND r->>'key'=%s""", (run_id, key))
    if section in {'followup', 'effectiveness'}:
        array_key = 'items' if section == 'followup' else 'groups'
        return (f"""WITH source AS (
            SELECT summary#>'{{strategy_lanes,{section}}}' AS v
              FROM quant.post_close_strategy_runs WHERE run_id=%s::uuid
          ), filtered AS (
            SELECT r,ordinality FROM source,LATERAL jsonb_array_elements(v->'{array_key}') WITH ORDINALITY t(r,ordinality)
             WHERE (%s::text IS NULL OR r->>'lane'=%s)
          ) SELECT (v-'{array_key}') || jsonb_build_object(
              '{array_key}',coalesce((SELECT jsonb_agg(r ORDER BY ordinality) FROM (
                SELECT r,ordinality FROM filtered ORDER BY ordinality LIMIT %s OFFSET %s) page),'[]'),
              'page_total',(SELECT count(*) FROM filtered),'offset',%s,'limit',%s) AS detail
            FROM source""", (run_id, key, key, limit, offset, offset, limit))
    if section == 'research':
        return ("""SELECT jsonb_build_object('review_groups',summary#>'{strategy_lanes,review_groups}',
          'review_plan',summary#>'{strategy_lanes,review_plan}',
          'review_coverage',summary#>'{strategy_lanes,review_coverage}',
          'review_policy',summary#>'{strategy_lanes,review_policy}') AS detail
          FROM quant.post_close_strategy_runs WHERE run_id=%s::uuid""", (run_id,))
    return ("""SELECT summary#>'{strategy_lanes,event_research}' AS detail
          FROM quant.post_close_strategy_runs WHERE run_id=%s::uuid""", (run_id,))


def read_detail(database: Any, *args):
    with database.transaction(statement_timeout_ms=15000) as connection:
        row = connection.execute(*detail_query(*args)).fetchone()
    return row['detail'] if row else None


async def read_detail_async(async_database: Any, *args):
    async with async_database.transaction() as connection:
        cursor = await connection.execute(*detail_query(*args))
        row = await cursor.fetchone()
    return row['detail'] if row else None
