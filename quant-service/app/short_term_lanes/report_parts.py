"""Shared human-readable sections for independent and consolidated reports."""
from __future__ import annotations
from .price_volume_report import lines as price_volume_lines


def stock(row: dict) -> str:
    return f"{row['name']}（{row['symbol'].split('.')[0]}）"


def context(result: dict) -> list[str]:
    c = result['coverage']
    lines = [f"数据日期：{result['as_of_date']}；扫描版本：{result['version']}。",
             f"完整历史：{c['complete_history']} / {c['universe']} 只主板股票。",
             '状态：扫描完成。' if result['status'] == 'completed' else '状态：数据不足，不能据此判断没有机会。', '']
    market = result.get('market', {})
    if market.get('up_fraction') is not None:
        lines += [f"共同市场背景：主板有效样本上涨占比 {market['up_fraction']:.1%}，十日收益中位数 {market['median_return10']:+.2f}%。这不是完整大盘研判。", '']
    regime = market.get('regime') or {}
    if regime.get('label'):
        lines += [f"策略路由：{regime['label']}，研究风险预算 {float(regime.get('research_budget',0)):.0%}；"
                  f"{'；'.join(regime.get('evidence') or [])}。该状态不代表上涨概率。", '']
    enrichment = result.get('history_enrichment') or {}
    config = result.get('governance_config') or {}
    if config.get('note'):
        lines += [config['note'], '']
    if enrichment.get('requested'):
        lines += [f"严格OHLC二阶段补充：预筛 {enrichment['requested']} 只，成功 {enrichment.get('ready',0)} 只，失败 {enrichment.get('failed',0)} 只；"
                  "高级策略只在补充范围内评估，不把它冒充全市场OHLC覆盖。", '']
    return lines


def review_sections(projection: dict) -> list[str]:
    lines = []
    decision = projection.get('decision_research') or {}
    if projection.get('review_plan'):
        c = projection['review_coverage']
        terminal = decision.get('terminal', 0)
        lines += ['## 为什么优先复核这些股票', '', projection['review_policy'], '',
                  f"首位代表计划 {c['planned']} 只；一手公司复核 {c['completed']} 只，结构化决策门禁终态 {terminal}/{decision.get('planned', c['planned'])}；两者分开计数。", '']
        for target in projection['review_plan']:
            if target['symbol'] in c['missing_symbols']:
                dossier = next((row for row in decision.get('dossiers', []) if row.get('symbol') == target['symbol']), None)
                if dossier and dossier.get('status') in {'passed','rejected'}:
                    lines += [f"- {stock(target)}：{target['selection_reason']} 已取得结构化决策门禁终态；一手公司复核是独立证据层，未用其他股票替代。", '']
                elif dossier:
                    lines += [f"- {stock(target)}：{target['selection_reason']} 决策门禁已明确终止为证据不足，不生成买入计划。", '']
                else:
                    lines += [f"- {stock(target)}：{target['selection_reason']} 本轮决策研究未执行，整张决策报告不得标为闭环。", '']
    if decision.get('dossiers'):
        lines += ['## 结构化决策门禁', '',
                  '门禁读取同轮量价、板块、估值和独立下行几何；通过也不等同自动交易或长期价值结论。', '']
        for dossier in decision['dossiers']:
            lines += [f"### {stock(dossier)}", '',
                      f"终态：{dossier.get('status')}。{dossier.get('conclusion','')}", '']
            for gate in dossier.get('gates', []):
                lines += [f"- {gate.get('gate_key')} {gate.get('label')} [{gate.get('verdict')}]：{gate.get('conclusion')}" ]
            lines += ['']
    for group in projection.get('review_groups', []):
        if not group['items']:
            continue
        lines += [f"## {group['label']}", '']
        for review in group['items']:
            selection = review.get('selection') or {}
            lines += [f"### {stock(review)}", '', f"复核结果：{review['outcome_label']}。{review['conclusion']}", '',
                      f"为什么复核：{review['selection_reason']}", '',
                      f"本次具体问题：{selection.get('question', '旧记录未记录，不补造')}", '',
                      f"研究动机：{selection.get('why_now', '旧记录未记录')}", '',
                      f"主营：{review['business']}", '', f"风险：{review['risk']}", '',
                      '证据：'+'；'.join(f"[{s['published_date']} 公司公告]({s['url']})" for s in review['sources']), '']
    return lines


def candidates(lane: dict) -> list[str]:
    lines = ['## 独立结构观察（不等于可买）', '', lane.get('observation_policy', ''), '']
    for row in lane.get('observation_list', []):
        heat = row.get('attention', {})
        lines += [f"### {stock(row)}", '', f"- 为什么观察：{row['reason']}。",
                  f"- 风险与执行：{row['caution']}。", f"- 等待确认：{row['confirmation']}。",
                  f"- 放弃条件：{row['invalidation']}。"]
        if heat:
            ratio = heat.get('amount_multiple')
            ratio_text = f'{ratio:.2f}倍' if ratio is not None else '未知'
            lines += [f"- 交易基础分 {row['liquidity']['score']:.0f}；成交参与度位于本策略{heat['sample_count']}个样本的{heat['participation_percentile']:.0f}分位，成交额/前5日均额{ratio_text}。热度与流动性分开显示，热度暂未改变正式排名。"]
        lines += ['']
    lines += ['## 条件观察明细', '',
             f"本策略匹配 {lane['total_matches']} 只，展示筛选后的 {len(lane['selected'])} 只；不是全量匹配名单。", '']
    if lane.get('factor_policy'):
        policy = lane['factor_policy']
        lines += ['可选排序因子已启用：'+policy['rule']+'。', '']
        for factor in policy['factors']:
            c = factor['context']
            lines += [f"- 小盘偏好：加分预算{factor['weight']*100:.0f}分（随策略风险倍率缩放）；"
                      f"有效市值覆盖{c['valid_count']}/{c['universe_count']}，日期{c['as_of_date']}；{c['cap_basis']}。"
                      + ('覆盖不足，本轮未加分。' if c['status']!='ready' else ''), '']
    if lane.get('status') == 'data_gap':
        gaps = '、'.join(f"{key}×{value}" for key,value in (lane.get('data_gaps') or {}).items()) or '所需严格历史不足'
        lines += [f"本策略当前为数据缺口状态：{gaps}。不会降低门槛或用收盘价伪造OHLC。", '']
    if not lane['selected']:
        lines += [lane['empty_reason'], '']
    for row in lane['selected']:
        m = row['metrics']
        lines += [f"### {stock(row)}", '', f"- 为什么观察：{row['reason']}。", f"- 行业：{row['sector_label']}。",
                  f"- 收盘 {m['close']:.2f}，涨跌 {m['change_pct']:+.2f}%；成交额 {m['amount']/1e8:.2f} 亿元，换手 {m['turnover']:.2f}%。",
                  f"- 确认条件：{row['confirmation']}。", f"- 放弃条件：{row['invalidation']}。",
                  f"- 当前注意：{row['caution']}。", f"- 有效期：{row['expiry']}。", '']
        lines += price_volume_lines(row.get('price_volume')) + ['']
        envelope = row.get('risk_envelope') or {}
        overlay = row.get('factor_overlay')
        if overlay:
            lines += [f"- {overlay['explanation']}。", f"- 排名范围：{overlay['rank_scope']}。", '']
            for factor in overlay['factors']:
                lines += [f"- {factor['explanation']}；本项加{factor['bonus']:.2f}分。", '']
        liq = row.get('liquidity') or {}
        if liq:
            baseline = f"{liq['median_amount_5d']/1e8:.2f}亿元" if liq.get('median_amount_5d') is not None else '历史不足，已折扣'
            lines += [f"- 交易活跃依据：近5日成交额中位数{baseline}，有效成交额{liq['effective_amount']/1e8:.2f}亿元；"
                      f"活跃度影响排序的{row['ranking_components']['liquidity_weight']:.0%}，不代表上涨概率。", '']
        if envelope:
            lines[-1:-1] = [f"- 风险预算：单股上限 {envelope['max_single_position_pct']:.1f}%，行业上限 {envelope['max_sector_exposure_pct']:.1f}%；"
                            f"结构失效参考 {envelope['failure_reference']:.2f}，并执行3日时间止损复核。",]
    lines += ['## 高拥挤或转弱观察', '']
    if not lane.get('caution_list'):
        lines += ['本轮没有展示的高拥挤或转弱样本。', '']
    for row in lane.get('caution_list', []):
        lines += [f"- {stock(row)}：{row['reason']}；{row['caution']}。"]
        lines += price_volume_lines(row.get('price_volume')) + ['']
    return lines + ['']


def limits(result: dict) -> list[str]:
    return ['## 方法和边界', '', result['notice'], '',
            '基础筛选价格来自实际收盘快照；高级策略使用Longhu前复权严格OHLC，不把收盘快照伪造成高低点或ATR。仅供下一交易日观察，收盘后重算。',
            '筛选尚无独立样本外盈利验证。各策略分数不可跨策略相加；新策略保持shadow/research-only，达到前向样本门槛前不输出胜率。',
            '统一风险层独立于alpha信号，约束单股、行业、组合预算、T+1、成本、结构/时间/跟踪退出；不自动修改券商自选、持仓或下单。', '']
