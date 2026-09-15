"""Bounded representative OHLC queue shared by baseline and quality analysis."""
from .advanced_strategies import prefilter_symbols


def symbols(rows, sessions, preliminary, *, limit=96):
    selected = []
    # Fair rounds across all lanes; include caution so adverse evidence is not hidden.
    pools = [lane.get('observation_list', []) + lane.get('selected', []) + lane.get('caution_list', []) for lane in preliminary['lanes']]
    for rank in range(max((len(pool) for pool in pools), default=0)):
        for pool in pools:
            if rank < len(pool) and pool[rank]['symbol'] not in selected:
                selected.append(pool[rank]['symbol'])
    for symbol in prefilter_symbols(rows, sessions, limit=limit):
        if symbol not in selected:
            selected.append(symbol)
    return selected[:limit]
