"""Bounded public research projection; inputs/account data never leave here."""
from copy import deepcopy


def latest(database,run_id=None,lane_key=None,symbol=None):
    with database.transaction() as c:
        rows=c.execute('''SELECT run_id,cutoff,state,stage,error,model_version,updated_at
            FROM quant.intraday_strategy_scans ORDER BY created_at DESC LIMIT 20''').fetchall()
        row=(c.execute('SELECT run_id,result FROM quant.intraday_strategy_scans WHERE run_id=%s AND state=%s',(run_id,'completed')).fetchone()
             if run_id else c.execute("SELECT run_id,result FROM quant.intraday_strategy_scans WHERE state='completed' AND model_version LIKE 'intraday-nine-%' ORDER BY cutoff DESC,created_at DESC LIMIT 1").fetchone())
        if not row:return dict(status='no_run',runs=rows,result=None)
        result=deepcopy(row['result']);details=None
        for lane in result['lanes']:
            for item in lane['items']:
                if symbol==item['symbol'] and (not lane_key or lane_key==lane['key']):details=deepcopy(item)
                item.pop('chart',None);item.pop('ohlc',None)
                # List requests do not repeatedly ship full factor series or
                # every candidate's chart. Detail is pinned to this run ID.
                for field in ('metrics','discovery_evidence','baseline','original_confirmation','original_invalidation'):
                    item.pop(field,None)
            lane['top']=lane['items'][:5]
        result['previous']=[]
        if lane_key:result['lanes']=[l for l in result['lanes'] if l['key']==lane_key]
        comparisons=c.execute('''SELECT source_run_id,close_run_id,result->>'source_cutoff' AS source_cutoff,
            result->>'close_cutoff' AS close_cutoff,jsonb_array_length(result->'items') AS compared
            FROM quant.intraday_scan_reconciliations WHERE close_run_id=%s OR source_run_id=%s ORDER BY created_at DESC LIMIT 20''',(row['run_id'],row['run_id'])).fetchall()
        changes=[]
        if symbol:
            records=c.execute('''SELECT source_run_id,result FROM quant.intraday_scan_reconciliations
                WHERE close_run_id=%s OR source_run_id=%s ORDER BY created_at DESC LIMIT 20''',(row['run_id'],row['run_id'])).fetchall()
            changes=[dict(source_run_id=str(r['source_run_id']),source_cutoff=r['result']['source_cutoff'],**i)
                     for r in records for i in r['result']['items'] if i['symbol']==symbol and (not lane_key or i['lane']==lane_key)]
    return dict(status='completed',run_id=str(row['run_id']),runs=rows,result=result,detail=details,reconciliations=comparisons,changes=changes)
