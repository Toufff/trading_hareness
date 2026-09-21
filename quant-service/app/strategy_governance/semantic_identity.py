"""Conservative lane-scoped decision identities; build identity remains separate.

Only literal lane branches are specialized. Unknown branches remain in the
digest, so ambiguity splits a cohort rather than silently merging behaviour.
No historical profile is rewritten or declared equivalent by this module.
"""
import ast
import hashlib
import json
from pathlib import Path

LANES = {'accumulation','expansion','pullback','trend','event','relay','contraction','rotation','reclaim'}
SHARED = ('rules.py','conditions.py','discovery.py','regime.py','risk.py','price_volume.py','event_time.py')


class _LaneTree(ast.NodeTransformer):
    def __init__(self, lane):
        self.lane = lane

    def visit_Expr(self, node):
        # Docstrings do not change executable behaviour.
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return None
        return self.generic_visit(node)

    def visit_Assign(self, node):
        # Human labels/purpose and a global report version are not scoring.
        if any(isinstance(t, ast.Name) and t.id in {'VERSION','BASE_LANES','ADVANCED_LANES','LANES'} for t in node.targets):
            return None
        if any(isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id == 'matches'
               and isinstance(t.slice, ast.Constant) and t.slice.value in LANES and t.slice.value != self.lane
               for t in node.targets):
            return None
        return self.generic_visit(node)

    def visit_Dict(self, node):
        if any(isinstance(k, ast.Constant) and k.value in LANES for k in node.keys):
            pairs = [(k,v) for k,v in zip(node.keys,node.values)
                     if not isinstance(k, ast.Constant) or k.value not in LANES or k.value == self.lane]
            node.keys, node.values = [p[0] for p in pairs], [p[1] for p in pairs]
        return self.generic_visit(node)

    def visit_If(self, node):
        test = node.test
        if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
            and test.left.id in {'lane','key','advanced_key'} and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq) and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value in LANES):
            branch = node.body if test.comparators[0].value == self.lane else node.orelse
            result = []
            for item in branch:
                new = self.visit(item)
                if new is not None:
                    result.extend(new if isinstance(new, list) else [new])
            return result
        return self.generic_visit(node)


def semantic_source(source, lane):
    return ast.dump(_LaneTree(lane).visit(ast.parse(source)), include_attributes=False)


def decision_versions(settings, app_root=None):
    root = Path(app_root) if app_root else Path(__file__).resolve().parents[1]
    versions = {}
    for lane in sorted(LANES):
        files = [root/'short_term_lanes'/name for name in SHARED]
        files += [root/'short_term_liquidity.py', root/'research_prices.py']
        if lane in {'contraction','rotation','reclaim'}:
            files.append(root/'short_term_lanes'/'advanced_strategies.py')
        if lane == 'accumulation':
            files.append(root/'short_term_lanes'/'accumulation_rules.py')
        active = [f for f in settings.get('ranking_factors',[]) if not f.get('strategies') or lane in f['strategies']]
        if active:
            files += sorted((root/'ranking_factors').glob('*.py'))
        sources = {p.relative_to(root).as_posix(): semantic_source(p.read_text(encoding='utf-8'), lane) for p in files}
        config = {**settings, 'ranking_factors': active}
        if lane != 'expansion':
            config.pop('expansion_multiple', None)
        if lane not in {'event', 'pullback'}:
            config.pop('pullback_multiple', None)
        versions[lane] = hashlib.sha256(json.dumps({'sources':sources,'settings':config,
            'price_contract':'canonical-snapshot-v1','execution_contract':'observation-not-trade-v1'},
            sort_keys=True,default=str).encode()).hexdigest()
    return versions


def lane_profile(result, lane):
    version = (result.get('decision_versions') or {}).get(lane)
    if version:
        value = {'schema':'lane-semantic-identity-v1','lane':lane,'decision_version':version}
    else:
        # Keep exactly the old formula when reading a historical run.
        value = {'version':result['version'],'settings':result.get('settings',{}),
                 'strategy_code_hash':result.get('strategy_code_hash','legacy_unverified')}
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
