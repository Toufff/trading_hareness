"""One overview and one report per lane from the identical immutable result."""
from pathlib import Path
import re
STATES={'platform_observation':'平台内潜伏观察','confirmed_observation':'结构转强','wait_confirmation':'等待承接','execution_uncertain':'涨停附近/成交未证实','invalidated':'结构转弱','data_gap':'缺分钟或同刻证据'}

def _reason(text, incomplete_day):
    text=(text or '').replace('|','/').replace('\n',' ')
    if incomplete_day:
        text=re.sub(r'成交额为前\d+日均值\d+(?:\.\d+)?倍',
                    '当日成交额未结算，待同刻基线或收盘后比较',text)
    return text


def table(items, *, incomplete_day=False):
    lines=['|股票|策略|状态|价格 / 涨幅|成交额|参考 / 失效结构|依据|','|---|---|---|---|---|---|---|']
    for r in items:
        price=f"{r['price']:.2f} / {r['change_pct']:+.2f}%" if r.get('price') else '—'
        amount=f"{r['amount']/1e8:.2f}亿" if r.get('amount') else '—'
        ref=f"{r['reference']:.2f}" if r.get('reference') else '—'
        support=f"{r['support']:.2f}" if r.get('support') else '—'
        date=(r.get('baseline') or {}).get('date') or (r.get('available_at') or '')[:10]
        reason=_reason(r.get('reason'),incomplete_day)
        if r.get('current_reason'):reason+='；本轮：'+_reason(r['current_reason'],incomplete_day)
        original=_reason(r.get('original_reason'),False)[:110]
        if r['source']=='previous':reason+='；原观察'+date+'：'+original
        from .rules import LABELS
        lines.append(f"|{r['name']}（{r['symbol'].split('.')[0]}）|{LABELS[r['lane']]}|{STATES[r['state']]}|{price}|{amount}|{ref} / {support}|{reason}|")
    return '\n'.join(lines)

def recommendation_table(items):
    lines=['|优先级|股票|午盘状态|价格 / 涨幅|原触发条件|原失效条件|','|---|---|---|---|---|---|']
    for r in items:
        price=f"{r['price']:.2f} / {r['change_pct']:+.2f}%" if r.get('price') else '—'
        trigger=(r.get('recommendation_trigger') or '—').replace('|','/').replace('\n',' ')
        invalidation=(r.get('recommendation_invalidation') or '—').replace('|','/').replace('\n',' ')
        lines.append(f"|{r.get('recommendation_priority','—')}|{r['name']}（{r['symbol'].split('.')[0]}）|{STATES[r['state']]}|{price}|{trigger}|{invalidation}|")
    return '\n'.join(lines)

def render(result):
    from ..event_research.report import sections as event_sections
    from .presentation import build as build_presentation
    out={};header=f"数据截止 {result['cutoff']}；模型 {result['version']}；输入 {result['input_hash']}。\n\n"
    presentation=result.get('presentation') or build_presentation(
        [r for lane in result['lanes'] for r in lane['items']], result['lanes'])
    incomplete_day=result['cutoff'][11:16]<'15:00'
    stock_table=lambda rows: table(rows,incomplete_day=incomplete_day)
    formal=presentation['formal_recommendations']
    current_count=sum(len(presentation['strategy_front'][lane['key']]) for lane in result['lanes'])
    summary='# 盘中多策略观察\n\n## 本轮结论\n\n'
    summary+=f"昨日正式推荐跟踪 {len(formal)} 只；本轮重新匹配且结构仍可观察的策略席位 {current_count} 个。"
    summary+='这是量价筛选与连续跟踪，不是新的跨策略推荐排序；公司研究尚未由本报告完成，不能据此直接买入。\n\n'
    summary+='## 昨日正式推荐跟踪（按正式优先级）\n\n'+(recommendation_table(formal) if formal else '昨日没有仍在有效观察窗口内的正式推荐。')+'\n\n'
    summary+='## 各策略当前前排（仅本轮重新匹配，按策略内部顺序）\n\n'
    for lane in result['lanes']:
        front=presentation['strategy_front'][lane['key']]
        summary+='### '+lane['label']+'\n\n'+(stock_table(front) if front else '本轮没有重新匹配且完成分钟确认的前排。')+'\n\n'
        current=[r for r in lane['items'] if r.get('matched_today') and r['state'] not in {'invalidated','data_gap'}]
        body='# '+lane['label']+'：盘中报告\n\n## 本轮策略结论\n\n'
        status={'completed':'已执行','partial':'部分覆盖','data_gap':'数据不足'}.get(lane.get('status'),lane.get('status','历史版本'))
        body+=f"本轮匹配 {lane.get('total_matches','未提供')}；以下仅是本轮重新匹配对象，不含只做延续跟踪的旧候选。"
        body+='公司研究尚未由本报告完成；结构观察不是买入授权。\n\n'
        body+='## 本轮重新匹配的重点股票\n\n'+(stock_table(current[:5]) if current else '本轮没有可列入重点的重新匹配对象。')+'\n\n'
        body+='## 量价条件与失效\n\n'
        for item in current[:5]:
            body+=f"### {item['name']}（{item['symbol'].split('.')[0]}）\n\n"
            body+=str(item.get('entry_scenario') or item.get('original_confirmation') or '原计划未提供结构化条件')+'\n\n'
            body+='；'.join(item.get('evidence_gaps',[]))+'\n\n'
        body+='## 原候选连续跟踪（不是本轮新增前排）\n\n'+stock_table([r for r in lane['items'] if r['source']=='previous' and not r.get('matched_today')])+'\n\n'
        body+='\n'.join(event_sections(result.get('event_research'),{r['symbol'] for r in lane['items']},max_events=8,max_leads=4))
        body+='## 范围与数据边界\n\n'+lane['discovery_scope']+'\n\n'
        body+=f"策略状态：{status}；本轮匹配 {lane.get('total_matches','未提供')}；数据缺口 {lane.get('data_gaps',{})}。\n\n"
        if any(lane.get('data_gaps',{}).values()):
            body+='完整日K按优先队列补充；未覆盖标的不视为不符合。当前匹配数只代表已验证范围，不能据此认定全市场没有机会。\n\n'
        body+=header+'## 附录：全部候选（含失败和缺证据）\n\n'+stock_table(lane['items'])+'\n'
        out[lane['key']]=body
    summary+='## 历史候选延续跟踪（不计入本轮前排）\n\n'
    for lane in result['lanes']:
        followed=presentation['strategy_followups'][lane['key']]
        if followed:summary+='### '+lane['label']+'\n\n'+stock_table(followed)+'\n\n'
    summary+='## 市场环境\n\n'
    m=result['market'];summary+=f"样本{m['symbols']}只，上涨{m['up']}、下跌{m['down']}，涨幅中位数{m['median']:+.2f}%。\n\n"
    relevant={r['symbol'] for r in formal}
    for lane in result['lanes']:
        relevant.update(r['symbol'] for r in presentation['strategy_front'][lane['key']])
    summary+='\n'.join(event_sections(result.get('event_research'),relevant,max_events=8,max_leads=4))
    plan=presentation.get('research_plan') or {}
    if plan.get('targets'):
        summary+='## 公司研究范围（尚待同轮复核）\n\n'
        for target in plan['targets']:
            peer=target.get('comparison_peer')
            comparison=f"；同组对照 {peer['name']}（{peer['symbol'].split('.')[0]}）" if peer else ''
            summary+=f"- {target['name']}（{target['symbol'].split('.')[0]}）：{'、'.join(target['selection_reasons'])}{comparison}。\n"
        summary+='\n本节是研究任务范围，不是新推荐排序；未完成主营、消息、量价、风险和比较复核前，不能把本报告称为完整午盘决策。\n\n'
    summary+='## 数据与边界\n\n'+header+f"历史结构截至{result['history_through']}。\n\n"
    summary+='选择合同：'+presentation['selection_contract']+'\n\n'
    summary+='九套正式策略独立重算，策略内按证据状态、正式策略排序和成交额展示，不比较跨策略分数。潜伏可以停留在平台内，不能一律要求先突破。高级形态只接受实际OHLC，覆盖和缺口见每策略状态。\n\n'
    summary+=f"实际可知时间 {result.get('observed_at',result['cutoff'])}；运行阶段 {result.get('phase','intraday')}；OHLC采集时间 {result.get('ohlc_captured_at','未补充')}。闭市初始化不得倒签为尾盘建议。\n\n"
    summary+='量比为供应商代理指标，历史同刻成交额不足时明确未知；不以半天/全天比值判断缩量。回踩、趋势等候选仍需进一步确认量价细节。\n\n'
    summary+='结构参考线不等于限价买单。新计划从实际采集完成后起算；未来价格触发与可成交、收益分开，受T+1和涨跌停约束。原文字计划保留，不擅自解析成历史成交。公司研究另行核查，结构确认不是盈利或买入授权。\n'
    out['overview']=summary
    return out

def write(result,directory):
    target=Path(directory);target.mkdir(parents=True,exist_ok=True)
    paths={}
    for key,text in render(result).items():
        path=target/(key+'.md');path.write_text(text,encoding='utf-8');paths[key]=str(path)
        if path.read_text(encoding='utf-8')!=text:raise ValueError('report readback mismatch')
    return paths
