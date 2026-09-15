"""State-aware watch conditions; closing references are never order prices."""


def watch_conditions(lane: str, metrics: dict) -> tuple[str, str]:
    close, high = metrics['close'], metrics['prior_high']
    low, ma5, ma10 = metrics['recent_low'], metrics['ma5'], metrics['ma10']
    if lane == 'relay':
        return (
            '次日能正常成交、分歧后回封且板块仍有同伴；高开直接封死不视为买入机会',
            '放量开板后反复回封失败，或同板块梯队明显转弱；不能成交则放弃，不用远处历史低点替代接力失效判断',
        )
    if lane == 'pullback':
        return (
            f'回踩后重新站稳{max(ma5, close):.2f}附近，板块未转弱，再看成交额是否恢复',
            f'放量跌破10日均线参考{ma10:.2f}后不能收复，或板块同步转弱；不把继续下跌当成缩量回踩',
        )
    if lane == 'trend':
        return (
            f'观察5日均线参考{ma5:.2f}附近的缩量承接；若直接上冲则等分歧，不因仍在趋势中就追高',
            f'5日均线失守后反抽不能收复且成交额放大，或行业强度消失；10日均线{ma10:.2f}仅为更长结构参考，不是允许一直持有的止损线',
        )
    if lane == 'contraction':
        advanced = metrics.get('advanced') or {}
        reference = advanced.get('prior10_high')
        support = advanced.get('prior10_low')
        if reference is None or support is None:
            return ('前10日真实高低点尚未核验，不用5日收盘高点替代突破线；补齐后再判断',
                    '缺少本次突破的同口径结构线，不能生成精确失效价')
        observed = '今日收盘已越过' if advanced.get('first_close_breakout') else '今日仅接近、尚未收盘越过'
        return (
            f'{observed}前10日真实高点{reference:.2f}；下一交易日观察突破后回踩守住该线，成交额恢复且板块不转弱；不是已验证买点',
            f'突破后跌回同一突破线{reference:.2f}下方且反抽不能收复，或放量跌破前10日真实低点{support:.2f}；后者不是允许一直持有的止损价',
        )
    if lane == 'rotation':
        return (
            '行业上涨广度和3日资金继续改善，个股不脱离板块单独加速；等待分歧后的承接',
            f'行业广度退回半数以下且资金转负，或个股放量跌破近期结构{low:.2f}',
        )
    if lane == 'reclaim':
        advanced = metrics.get('advanced') or {}
        reference, panic_low = advanced.get('reclaim_reference'), advanced.get('panic_low')
        if reference is None or panic_low is None:
            return ('急跌前结构与本次修复线未核验，不能以普通5日均线代替修复确认',
                    '缺少本次急跌低点，不生成虚假的精确失效位')
        return (
            f'观察急跌前参考{reference:.2f}的收复与守稳；下一交易日核验反弹成交及行业承接，日线修复不是盘中已确认买点',
            f'再次跌破本次急跌日真实低点{panic_low:.2f}且不能收复，或新出现已核验公司利空；须考虑T+1和无法成交风险',
        )
    if close > high:
        return (
            f'今天已突破前5日收盘平台{high:.2f}；下一交易日观察回踩该平台的承接，或在其上缩量整理后再放量，不把已发生的突破写成未来买点',
            f'跌回突破平台{high:.2f}下方且反抽不能收复，或放量下跌、板块转弱，则本次突破预期失效',
        )
    return (
        f'有效突破{high:.2f}附近的前5日收盘平台，成交额与板块同时改善',
        f'跌破近期收盘支撑{low:.2f}且不能收复，或板块转弱；这只是平台观察失效条件，不是预设买单止损',
    )
