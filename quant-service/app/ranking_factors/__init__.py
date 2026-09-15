"""Opt-in, strategy-scoped ranking overlays. Matches and risk remain upstream.

Register additional pure factor context builders here; factors must not fetch
data or mutate candidate eligibility. Every profile is serialized with results.
"""
from dataclasses import dataclass
import json
from math import isfinite
from pathlib import Path
from . import small_cap

REGISTRY = {'small_cap': small_cap.context}


@dataclass(frozen=True)
class FactorSpec:
    key: str
    weight: float = .12
    strategies: tuple[str, ...] = ('*',)

    def __post_init__(self):
        if self.key not in REGISTRY:
            raise ValueError(f'Unknown ranking factor: {self.key}')
        if (isinstance(self.weight, bool) or not isinstance(self.weight, (int,float))
                or not isfinite(self.weight) or not 0 <= self.weight <= .25):
            raise ValueError('Factor weight must be finite and in [0,0.25]')
        if (not isinstance(self.strategies, tuple) or not self.strategies
                or any(not isinstance(s, str) or not s for s in self.strategies)):
            raise ValueError('Factor strategies must be a nonempty tuple of lane keys')
        if '*' in self.strategies and self.strategies != ('*',):
            raise ValueError('Use either * or explicit lane keys, not both')


def validate(specs, lane_keys):
    seen = set()
    for spec in specs:
        if spec.key in seen:
            raise ValueError(f'Duplicate factor: {spec.key}')
        seen.add(spec.key)
        if set(spec.strategies) - set(lane_keys) - {'*'}:
            raise ValueError(f'Unknown factor strategy: {spec.strategies}')
    for lane in lane_keys:
        if sum(s.weight for s in specs if '*' in s.strategies or lane in s.strategies) > .25:
            raise ValueError('Combined factor bonus budget exceeds 25 points')


def load_profile(payload, lane_keys):
    if isinstance(payload, (str, Path)):
        payload = json.loads(Path(payload).read_text(encoding='utf-8-sig'))
    if not isinstance(payload, dict) or set(payload) - {'version','factors'}:
        raise ValueError('Profile must contain only version and factors')
    if payload.get('version', 1) != 1 or not isinstance(payload.get('factors', []), list):
        raise ValueError('Unsupported factor profile version or factors format')
    specs = []
    for item in payload.get('factors', []):
        if not isinstance(item, dict) or set(item) - {'key','weight','strategies','enabled'}:
            raise ValueError('Unknown factor config field')
        if not isinstance(item.get('enabled', True), bool):
            raise ValueError('enabled must be boolean')
        if not isinstance(item.get('strategies', ['*']), list):
            raise ValueError('strategies must be an array')
        spec = FactorSpec(item.get('key'), item.get('weight', .12), tuple(item.get('strategies', ['*'])))
        validate((spec,), lane_keys)
        if item.get('enabled', True):
            specs.append(spec)
    validate(specs, lane_keys)
    return tuple(specs)


def build_contexts(specs, universe, as_of_date):
    return {s.key: REGISTRY[s.key](universe, as_of_date) for s in specs if s.weight > 0}


def apply_factors(rows, lane, specs, contexts):
    active = [s for s in specs if s.weight > 0 and ('*' in s.strategies or lane in s.strategies)]
    if not active:
        return None  # exact backward-compatible no-op
    groups = {}
    for row in rows:
        groups.setdefault(row.get('state', 'watch'), []).append(row)
    for group in groups.values():
        base = sorted(group, key=lambda r: (-r.get('factor_overlay', {}).get('base_score', r['rank_score']), r['symbol']))
        for rank, row in enumerate(base, 1):
            original = row.get('factor_overlay', {}).get('base_score', row['rank_score'])
            contributions = []
            bonus = 0.
            for spec in active:
                c = contexts[spec.key]
                e = c['evidence'].get(row['symbol'])
                status = 'applied' if c['status'] == 'ready' and e else 'missing' if c['status'] == 'ready' else c['status']
                value = spec.weight * e['score'] * row.get('regime_route', {}).get('priority_weight', 1) if status == 'applied' else 0.
                bonus += value
                contributions.append(dict(key=spec.key, version=c['version'], status=status,
                      weight=spec.weight, bonus=round(value, 4), evidence=e,
                      explanation=e['explanation'] if status == 'applied' else '市值数据缺失、过期或参考覆盖不足；未加分，不改变原策略条件'))
            row['rank_score'] = round(original + bonus, 4)
            row['factor_overlay'] = dict(base_score=original, base_rank=rank,
                adjusted_score=row['rank_score'], bonus=round(bonus, 4), factors=contributions,
                rank_scope='同策略、同风险状态内全部匹配；行业限额与展示截断之前')
        for rank, row in enumerate(sorted(group, key=lambda r: (-r['rank_score'], r['symbol'])), 1):
            o = row['factor_overlay']
            o.update(adjusted_rank=rank, rank_change=o['base_rank']-rank,
                     explanation=f"可选排序因子：原排名{o['base_rank']} → 调整后{rank}，加{o['bonus']:.2f}分；只调整研究顺序，不是买入信号")
    return dict(enabled=True, factors=[dict(key=s.key, weight=s.weight,
                    context={k:v for k,v in contexts[s.key].items() if k!='evidence'}) for s in active],
                rule='原策略筛选后加分；不放宽成交、形态、风险和行业限额；缺数据不加分')
