"""Human summary before raw leads, shared by independent reports and UI."""
LABELS={'analyzed':'已完成有界消息研究','leads_only':'仅线索，语义研究未完成','failed':'消息链失败',
    'no_news':'来源没有返回消息','absent':'没有可用快照'}

def sections(value,symbols=None):
    if not value:return []
    lines=['','## 消息变化与方向影响','',LABELS.get(value.get('status'),str(value.get('status'))),
        value.get('summary',''),'']
    if value.get('stale'):lines+=['消息快照已过期，以下只能作为历史背景。','']
    analysis=value.get('analysis',{})
    if analysis.get('status')=='failed':
        lines += [f"失败阶段：{analysis.get('failure_stage','旧回执未细分')}；错误码：{analysis.get('failure_code','未记录')}。内容校验失败不等于连接失败。",'']
    for e in value.get('events',[]):
        if symbols is not None and e['category'] not in ('macro','policy') and not any(s['symbol'] in symbols for s in e['symbols']):continue
        lines += [f"### {e['fact']}",'',f"- 预期：{e['expectation']}；预期差：{e['surprise']}。",
            f"- 传导与时间：{e['transmission']}；{e['horizon']}。",
            f"- 观察处理：{e['action']}。",f"- 反证与失效：{e['counterevidence']}；{e['invalidate']}。"]
        for s in e['symbols']:lines += [f"- {s['name']}（{s['symbol'].split('.')[0]}）：{s['relation']}；方向 {s['direction']}。"]
        for s in e['sources']:
            label=f"{s['source']} {s['published_at']}"
            lines += [f"- 来源：[{label}]({s['url']})" if s['url'] else f"- 来源：{label}；证据ID {s['document_id'][:12]}（供应商未给原文链接）。"]
        lines+=['']
    leads=value.get('leads',[])
    if symbols is not None:leads=[l for l in leads if any(s['symbol'] in symbols for s in l['symbols'])]
    lines+=['### 线索附录（不是已核查利好）','']
    for lead in leads[:12]:
        lines += [f"- {lead['title']}："+'、'.join(f"{s['name']}（{s['symbol'].split('.')[0]}）" for s in lead['symbols'])+f"；{lead['published_at']}。"]
    lines+=['',f"消息轮次：{value.get('run_id','无')}；可知截止：{value.get('cutoff','未知')}。",
        '研究覆盖为有界快讯窗口，不代表全部消息；消息未自动改变策略权重或授予买入权限。','']
    return lines
