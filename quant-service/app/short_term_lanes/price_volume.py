"""Observable daily price/amount quality, independent from eligibility and liquidity.

Thresholds below are versioned research hypotheses, not backtest-optimal values.
No minute bars, executions, institutional identity or free-float are inferred.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from statistics import fmean

VERSION = 'price-volume-evidence-2026-09-11'


@dataclass(frozen=True)
class PriceVolumeSettings:
    pullback_contraction_ratio: float = 0.90
    low_close_location: float = 0.45
    high_upper_wick_fraction: float = 0.45
    high_amount_multiple: float = 1.50
    churn_max_change_pct: float = 1.00
    weak_change_pct: float = -2.00
    reclaim_min_recovery_fraction: float = 0.33
    reclaim_min_change_pct: float = 1.00
    reclaim_min_close_location: float = 0.60
    close_match_tolerance_pct: float = 0.10

    def __post_init__(self):
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                raise ValueError(f'Invalid finite price-volume parameter: {name}')
        if not 0 < self.pullback_contraction_ratio < 1:
            raise ValueError('Contraction threshold must be strictly less than one')
        if not all(0 < value < 1 for value in (self.low_close_location, self.high_upper_wick_fraction,
                 self.reclaim_min_recovery_fraction, self.reclaim_min_close_location)):
            raise ValueError('Price position/recovery fractions must be between zero and one')
        if (self.high_amount_multiple <= 1 or self.churn_max_change_pct <= 0
                or self.weak_change_pct >= 0 or self.reclaim_min_change_pct <= 0
                or self.close_match_tolerance_pct <= 0):
            raise ValueError('Invalid direction/scale for price-volume parameter')


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _day(value):
    return str(value or '')[:10].replace('-', '')


def segment_evidence(series: list[dict]) -> dict:
    """Close-defined advance into latest peak vs subsequent pullback, per-day means.

    This is an 11-close local segment, NOT an inferred intraday wave. Consecutive
    rising days preceding the last prior peak form the advance. Flat/down day
    terminates that run. A short segment is disclosed rather than extrapolated.
    """
    empty = {'advance_sessions': 0, 'pullback_sessions': 0,
             'advance_mean_amount': None, 'pullback_mean_amount': None,
             'pullback_amount_ratio': None}
    if len(series) < 3:
        return empty
    closes = [_number(row.get('close')) for row in series]
    amounts = [_number(row.get('amount')) for row in series]
    if any(v is None or v <= 0 for v in closes + amounts):
        return empty
    peak = max(range(len(closes)-1), key=lambda i: (closes[i], i))
    if peak == 0 or closes[-1] >= closes[peak]:
        return empty
    start = peak
    while start > 0 and closes[start] > closes[start-1]:
        start -= 1
    advance = amounts[start+1:peak+1]
    pullback = amounts[peak+1:]
    if not advance or not pullback:
        return empty
    return {'advance_sessions': len(advance), 'pullback_sessions': len(pullback),
            'advance_mean_amount': fmean(advance), 'pullback_mean_amount': fmean(pullback),
            'pullback_amount_ratio': fmean(pullback)/fmean(advance)}


def daily_bar(rows: list[dict], as_of_date: str, expected_close: float,
              settings: PriceVolumeSettings) -> tuple[dict | None, str | None]:
    candidates = [r for r in rows if _day(r.get('date') or r.get('trade_date')) == _day(as_of_date)]
    if len(candidates) != 1:
        return None, '缺少同交易日唯一日线，未验证收盘位置和上影'
    row = candidates[0]
    values = {k: _number(row.get(k)) for k in ('open','high','low','close')}
    if any(v is None or v <= 0 for v in values.values()):
        return None, '当日日线价格字段无效，未验证收盘质量'
    o,h,l,c = [values[k] for k in ('open','high','low','close')]
    if not l <= min(o,c) <= max(o,c) <= h:
        return None, '当日日线高低开收不一致，未验证收盘质量'
    if abs(c/expected_close-1)*100 > settings.close_match_tolerance_pct:
        return None, '日线与资金快照收盘价不一致，未混用不同价格口径'
    return {**row, **values}, None


def assess(lane: str, base: dict, rows: list[dict], as_of_date: str, *,
           advanced: dict | None = None, settings: PriceVolumeSettings = PriceVolumeSettings()) -> dict:
    metrics = {'amount_multiple': base.get('amount_multiple'), 'volume_multiple': None,
               'close_location': None, 'upper_wick_fraction': None,
               'close_return10_pct': base.get('return_10d'), **segment_evidence(base.get('flow_series', []))}
    refs = {'close_platform_high_5d': base.get('prior_high'),
            'recent_close_low': base.get('recent_low')}
    reasons, warnings = [], []
    unverified = ['intraday_order', 'execution', 'institutional_identity']
    close_series = base.get('flow_series', [])
    up_amounts, down_amounts = [], []
    for previous, current in zip(close_series, close_series[1:]):
        if current['close'] > previous['close']:
            up_amounts.append(current['amount'])
        elif current['close'] < previous['close']:
            down_amounts.append(current['amount'])
    metrics.update(up_day_mean_amount=fmean(up_amounts) if up_amounts else None,
                   down_day_mean_amount=fmean(down_amounts) if down_amounts else None,
                   down_up_amount_ratio=fmean(down_amounts)/fmean(up_amounts) if up_amounts and down_amounts else None)
    bar, error = daily_bar(rows, as_of_date, base['close'], settings)
    adverse = False
    if error:
        warnings.append(error)
        unverified.append('daily_ohlc_quality')
    else:
        span = bar['high'] - bar['low']
        if span > 0:
            metrics.update(close_location=(bar['close']-bar['low'])/span,
                           upper_wick_fraction=(bar['high']-max(bar['open'],bar['close']))/span)
            reasons.append(f"收盘位于当日价格区间的{metrics['close_location']:.0%}，上影占当日振幅{metrics['upper_wick_fraction']:.0%}")
            if (metrics['close_location'] < settings.low_close_location
                    and metrics['upper_wick_fraction'] >= settings.high_upper_wick_fraction):
                warnings.append('冲高后的价格成果保留较少，不能把成交活跃直接视为上涨质量好')
                adverse = True
        else:
            unverified.append('unrestricted_daily_range')
            warnings.append('当日高低价相同；限价或无波动日不能按普通收盘区间确认承接')
        # Only compare explicitly declared, homogeneous share-volume units.
        prior = sorted([r for r in rows if _day(r.get('date') or r.get('trade_date')) < _day(as_of_date)],
                       key=lambda r: _day(r.get('date') or r.get('trade_date')))[-5:]
        unit = bar.get('volume_unit')
        vols = [_number(r.get('volume')) for r in prior+[bar]]
        if (len(prior) == 5 and unit in {'shares','lots_100'}
                and all(r.get('volume_unit') == unit for r in prior)
                and all(v is not None and v > 0 for v in vols)):
            metrics['volume_multiple'] = vols[-1]/fmean(vols[:-1])
    if metrics['volume_multiple'] is None:
        unverified.append('share_volume_multiple')
    amount = base.get('amount_multiple', 0)
    change = base.get('change_pct', 0)
    if amount >= settings.high_amount_multiple and abs(change) <= settings.churn_max_change_pct:
        warnings.append('成交额明显扩大但收盘推进有限，属于分歧提示，不能据此断定吸筹或派发')
        adverse = True
    if amount >= settings.high_amount_multiple and change <= settings.weak_change_pct:
        warnings.append('成交额扩大伴随明显下跌，卖压尚未得到价格修复验证')
        adverse = True
    if lane == 'pullback':
        ratio = metrics['pullback_amount_ratio']
        contracted = ratio is not None and ratio <= settings.pullback_contraction_ratio
        metrics['pullback_amount_contracted'] = contracted if ratio is not None else None
        if ratio is None:
            unverified.append('advance_pullback_segments')
            warnings.append('无法从现有收盘路径划分上涨段和回调段，不能确认缩量回踩')
        else:
            reasons.append(f"收盘路径上涨段{metrics['advance_sessions']}日、回调段{metrics['pullback_sessions']}日；回调日均成交额/上涨段日均成交额为{ratio:.2f}倍")
            if not contracted:
                warnings.append('回调段成交额没有明显收缩，仅保留结构回撤候选，不能称已确认缩量')
                adverse = True
        unverified.append('pullback_stabilization')
    if lane == 'accumulation':
        reasons.append('资金合计和横盘形态分别成立不等于吸筹完成，需结合本次量价警示')
        unverified.append('long_horizon_position')
        if metrics['down_up_amount_ratio'] is not None:
            reasons.append(f"近10个收盘变化中，下跌日日均成交额/上涨日日均成交额为{metrics['down_up_amount_ratio']:.2f}倍；不是主力净买入")
    if lane == 'relay':
        unverified.append('limit_board_process')
        warnings.append('仅为日线涨停附近候选；未验证封板时间、开板次数、回封与次日可成交性')
    if lane == 'rotation':
        reasons.append('行业广度与资金改善是方向证据，个股收盘质量单独核验，不能相互替代')
    if advanced:
        for key in ('prior10_high','prior10_low','reclaim_reference','panic_low'):
            if key in advanced:
                refs[key] = advanced[key]
        for key in ('first_close_breakout','recovery_fraction','latest_change','amount_expansion'):
            if key in advanced:
                metrics[key] = advanced[key]
        if lane == 'contraction' and not advanced.get('first_close_breakout'):
            warnings.append('尚未收盘越过本次前10日高点，只是接近压力，不是首次有效突破')
            adverse = True
        if lane == 'reclaim' and not advanced.get('meaningful_recovery'):
            warnings.append('急跌后修复比例或收盘质量不足，仅为弱反弹候选，不称强修复')
            adverse = True
    critical_unknown = error or lane == 'relay' or 'advance_pullback_segments' in unverified or 'unrestricted_daily_range' in unverified
    status = 'quality_warning' if adverse else 'quality_unverified' if critical_unknown else 'verified_daily'
    return {'version': VERSION, 'status': status, 'quality_confirmed': status == 'verified_daily',
            'scope': '仅日线可观察量价条件；不是完整买入确认或盈利保证',
            'metrics': metrics, 'references': refs, 'reasons': reasons, 'warnings': warnings,
            'unverified': unverified, 'parameters': asdict(settings),
            'change_ids': ['PV01','PV02', {'pullback':'PV03','accumulation':'PV04',
                'contraction':'PV05','reclaim':'PV06','rotation':'PV07','relay':'PV07'}.get(lane,'PV02')]}
