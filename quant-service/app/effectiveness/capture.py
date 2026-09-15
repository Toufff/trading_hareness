"""Freeze bounded pre-outcome features; no retrospective feature reconstruction."""
from .rules import numeric


def features(item,lane):
    if item.get('effectiveness_features') is not None:
        return dict(item['effectiveness_features'])
    m=item.get('metrics') or {};a=m.get('accumulation') or {}
    # tracking_candidates omits accumulation; look up the observation payload
    # before freezing when available, otherwise explicitly leave scores absent.
    out={k:numeric(m.get(k)) for k in ('return_5d','return_10d','amount_multiple','close','ma5')}
    out['ma_gap_pct']=(out['close']/out['ma5']-1)*100 if out['close'] and out['ma5'] else None
    out['flow_score']=numeric((a.get('flow') or {}).get('score'))
    out['sideways_score']=numeric((a.get('sideways') or {}).get('score'))
    out['structure_score']=numeric(item.get('raw_score'))
    out['liquidity_score']=numeric((item.get('liquidity') or {}).get('score'))
    return out
