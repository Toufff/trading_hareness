"""Human-readable appendix from persisted thesis evaluations, never a new score."""
LABELS = {'pending': '待验证', 'supported': '结构条件获支持', 'challenged': '原条件受挑战',
          'invalidated': '已失效', 'expired': '期限已到', 'superseded': '已被修订替代',
          'complete': '完整', 'partial': '部分可验证', 'stale': '过时', 'conflict': '冲突',
          'waiting': '等待', 'eligible': '条件满足，非下单授权', 'suspended': '暂停新增',
          'cancelled': '已取消', 'unknown': '完整入场条件未确认'}


def safe(value):
    return str('—' if value is None or value == '' else value).replace('|', '／').replace('\n', ' ')


def sections(receipt, lane=None):
    if not receipt:
        return []
    lines = ['', '## 原始假设与本轮变化（影子评价，不改推荐排序）', '',
             '保留最初发现理由，不把今日排名下降直接解释成卖出；下列研究参考线不是账户止损。', '',
             f"评价截点：{safe(receipt.get('cutoff_at'))}；行情日期：{safe(receipt.get('data_date'))}；状态：{safe(receipt.get('status'))}。", '']
    if receipt.get('status') == 'failed':
        return lines + [f"本轮跟踪阶段失败：{safe(receipt.get('error_type'))}。其他扫描结论不被清空，不能把旧评价冒充本轮。", '']
    lines += ['| 股票 | 原始理由与结构 | 本轮量价事实 | 假设 / 新买 | 下一验证 |', '|---|---|---|---|---|']
    for item in receipt.get('items', []):
        if lane and not any(r.get('lane') == lane for r in item.get('original_rankings', []) + item.get('current_rankings', [])):
            continue
        states = item['states']; structure = item.get('original_structure') or {}
        obs = {o['metric']: o for o in item.get('observations', [])}
        facts = []
        if any(c.get('directly_comparable') is False for c in item.get('changes', [])):
            facts.append('数据口径/版本修正，前后数值不可直接解释为行情变化')
        if item.get('holding', {}).get('status') not in (None, 'unbound'):
            facts.append(item['holding']['note'])
        for metric, label, divisor, suffix in [('close', '收盘', 1, '元'), ('amount', '成交额', 1e8, '亿元'),
                ('amount_ratio_previous', '较前日', 1, '倍'), ('amount_ratio_mean5', '较前5日均额', 1, '倍'),
                ('main_net5', '5日供应商主力净额', 1e8, '亿元')]:
            if metric in obs and isinstance(obs[metric].get('value'), (int, float)):
                facts.append(f"{label}{obs[metric]['value']/divisor:.2f}{suffix}")
        for metric, label, divisor, suffix in [('price', '盘中价', 1, '元'),
                ('amount_so_far', '盘中累计额', 1e8, '亿元'), ('vwap', '盘中均价', 1, '元')]:
            if metric in obs and obs[metric].get('basis') == 'forming_intraday' and isinstance(obs[metric].get('value'), (int, float)):
                facts.append(f"{label}{obs[metric]['value']/divisor:.2f}{suffix}（{safe(obs[metric].get('effective_at'))}）")
        lines.append(f"| {safe(item.get('name'))}（{safe(item['symbol'])}） | {safe(item.get('claim'))}；原下沿{safe(structure.get('support'))}、上沿{safe(structure.get('reference'))} | {'；'.join(facts) or '本轮无可用当前行情'} | {LABELS.get(states['thesis_state'],states['thesis_state'])} / {LABELS.get(states['entry_state'],states['entry_state'])} | {safe(item.get('original_confirmation'))} |")
    lines += ['', '持仓动作：仍按已绑定且有效的纪律计划单独评价；本表不反推入场意图或生成卖出股数。',
              f"同轮source_run_id：`{safe(receipt.get('source_run_id'))}`；评价ID与证据哈希可在交易假设面板或CLI读取。", '']
    if receipt.get('failures'):
        lines += ['部分失败：' + '；'.join(f"{safe(x['symbol'])} {safe(x['error_type'])}" for x in receipt['failures']), '']
    return lines
