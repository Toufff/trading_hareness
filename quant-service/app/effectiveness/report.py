"""Same persisted effectiveness section in overview and each strategy report."""
STATUS={'exploratory':'历史补录，仅供探索','insufficient':'前瞻样本不足','sufficient':'可检查异常'}


def sections(value,lane=None):
    from ..short_term_lanes.rules import LANES
    labels={key:label for key,label,_ in LANES}
    if not value:return ['','## 策略效果反馈','','本轮未接入效果评估，不代表策略有效。']
    if value['status']!='completed':return ['','## 策略效果反馈','',value.get('reason','效果评估失败')]
    groups=[g for g in value['groups'] if not lane or g['lane']==lane]
    lines=['','## 策略效果反馈','',f"发现 {sum(bool(g['finding']) for g in groups)} 项充分样本异常；仅进入独立复核，不自动改策略。",
        '','| 策略 / 来源 | 状态 | 独立日期 | 前排减后排 |','|---|---|---:|---:|']
    for g in groups:
        delta=g['top_minus_rest_pp'];diff='暂无' if delta is None else f'{delta:+.2f} 个百分点'
        source={'manual':'人工建议','machine':'扫描','machine_legacy':'旧记录'}.get(g['source_kind'],'其他来源')
        lines.append(f"| {labels.get(g['lane'],g['lane'])} / {source} / {g['profile'][:8]} | {STATUS[g['status']]} | {g['independent_sessions']}/{g['minimum_sessions']} | {diff} |")
    lines+=['',value['notice'],'影子排序对照不是正式策略；通过未来样本、可成交性和费用核查后仍须独立复核及人工批准。',
        value.get('manual_notice','')]
    return lines
