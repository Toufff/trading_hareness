"""Render computed evidence, never turn future watch conditions into facts."""


def lines(evidence: dict | None) -> list[str]:
    if not evidence:
        return ['- 量价核对：该历史版本未记录结构化量价证据，不追认通过。']
    labels = {'verified_daily': '已核对日线证据', 'quality_warning': '存在量价警示',
              'quality_unverified': '证据不完整，仍是观察候选'}
    out = ['- 量价核对：' + labels.get(evidence.get('status'), '未确认') + '；不代表买入授权。']
    for reason in evidence.get('reasons', []):
        out.append(f'- 已计算的依据：{reason}')
    for warning in evidence.get('warnings', []):
        out.append(f'- 量价警示：{warning}')
    labels = {'intraday_order':'盘中触发的先后顺序','execution':'真实成交及可成交性',
        'institutional_identity':'买卖方是否为机构或主力','daily_ohlc_quality':'同日高低开收的质量',
        'unrestricted_daily_range':'无波动或限价日的正常承接','share_volume_multiple':'同口径成交股数的放缩量',
        'advance_pullback_segments':'可比较的上涨段与回调段','pullback_stabilization':'回踩后的持续企稳',
        'long_horizon_position':'更长周期的高低位置','limit_board_process':'封板、开板与回封过程'}
    for gap in evidence.get('unverified', []):
        out.append(f'- 尚不能确认：{labels.get(gap,"未识别的证据缺口（请检查版本契约）")}')
    return out
