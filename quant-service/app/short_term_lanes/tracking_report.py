"""Human-first follow-up, independent from fresh scan display limits."""
LABELS={'both_touched_order_unknown':'上、下参考线均触及，日内先后未确认',
        'reference_low_broken':'曾跌破原下参考线','reference_high_touched':'曾触及原上参考线',
        'no_reference_touch':'未触及原参考线','no_data':'暂无后续行情'}


def sections(followup, lane=None):
    if not followup:return []
    lines=['','## 往期候选跟踪与验证','',followup.get('note',''),'',f"跟踪状态：{followup['status']}。",'']
    items=[e for e in followup.get('items',[]) if (not lane or e['lane']==lane) and e['expected_sessions']>0]
    if not items:return lines+['尚无到期记录；不计为零收益或成功。','']
    lines+=['| 股票 | 发现日 / 原展示位 | 1日 / 3日 / 5日 / 10日表现 | 原结构检查 | 证据 |',
            '|---|---|---|---|---|']
    def value(w):
        return f"{w['return_pct']:+.2f}%" if w['status']=='observed' else '未到期' if w['status']=='not_due' else '缺数据'
    featured=[e for e in items if e.get('display_rank') is not None]
    background=[e for e in items if e.get('display_rank') is None]
    for e in featured:
        lines += [f"| {e['name']}（{e['symbol'].split('.')[0]}） | {e['signal_date']} / {e.get('display_rank') or '非首屏'} | "
                  +' / '.join(value(e['windows'][str(h)]) for h in (1,3,5,10))
                  +f" | {LABELS.get(e['path_check'],e['path_check'])} | {'历史补录' if e['timing']=='reconstructed' else '前瞻记录'}；{e['status']} |"]
    ledger=followup.get('ledger_rows')
    merged=f"账本原始记录{ledger}条，按股票、发现日、策略合并后展示{followup.get('total',len(items))}条；" if ledger else ''
    lines+=['',f'{merged}另有{len(background)}条非首屏匹配持续跟踪，完整记录保存在同轮JSON及G盘观察账本；未删除落榜或下跌样本。',
            '以上为相对发现日收盘的走势，不是买入收益。触线不证明原条件全部成立；不将涨停计为可买入，不将跌停假定可止损。','']
    return lines
