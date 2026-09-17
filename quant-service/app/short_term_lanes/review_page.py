"""Human-first post-close review page rendered from the persisted run.

One self-contained HTML file per settled day, built only from what the owner
already stores (strategy lanes, recommendation pool, followup, sector
overview, event research). Selection leads; market and news are background.
No narrative is invented: free text comes from the published decision and the
company reviews, everything else is data.
"""
from __future__ import annotations

import html
import json
import os
import tempfile
from pathlib import Path

DECISION_LABEL = {'recommend': '推荐', 'observe': '观察', 'exclude': '排除'}
STATE_LABEL = [('selected', '条件观察'), ('observation_list', '结构观察'), ('caution_list', '风险观察')]
PATH_LABEL = {'both_touched_order_unknown': '上、下参考线均触及', 'reference_low_broken': '曾跌破原下参考线',
              'reference_high_touched': '曾触及原上参考线', 'no_reference_touch': '未触及原参考线', 'no_data': '暂无后续行情'}


def esc(value) -> str:
    return html.escape('' if value is None else str(value))


def code(symbol: str) -> str:
    return str(symbol or '').split('.')[0]


def stock(row: dict) -> str:
    return f"{esc(row.get('name'))}（{esc(code(row.get('symbol')))}）"


def pct(value, digits: int = 2) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return '—'
    return f"{v:+.{digits}f}%"


def pct_cell(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return '<span>—</span>'
    return f'<span class="{"up" if v > 0 else "dn" if v < 0 else ""}">{v:+.2f}%</span>'


def yi(value) -> str:
    try:
        return f"{float(value) / 1e8:.1f}亿"
    except (TypeError, ValueError):
        return '—'


def parts_of(payload: dict) -> dict:
    """Accept the /post-close/latest response, a bare run, or already split parts."""
    if 'strategy_lanes' in payload:
        return payload
    run = payload.get('run') or payload.get('latest_completed') or payload
    summary = run.get('summary') or {}
    return {'run_id': run.get('run_id'), 'strategy_lanes': summary.get('strategy_lanes') or {},
            'recommendation_pool': summary.get('recommendation_pool') or {}}


def render(payload: dict) -> str:
    parts = parts_of(payload)
    lanes_result = parts['strategy_lanes']
    pool = parts.get('recommendation_pool') or {}
    day = lanes_result.get('as_of_date') or pool.get('as_of_date') or ''
    run_id = str(parts.get('run_id') or lanes_result.get('run_id') or '')
    sections = [_masthead(day, run_id, lanes_result, pool), _nav(pool),
                _verdict(lanes_result, pool), _pool(pool, lanes_result), _research(lanes_result),
                _lanes(lanes_result), _market(lanes_result), _news(lanes_result), _tracking(lanes_result),
                _system(day, run_id, lanes_result, pool),
                '<p class="note">以上基于公开信息与系统扫描结果。评分不是收益率或上涨概率，研究完成不等于买入授权，不构成投资建议。</p>']
    return (f'<title>{esc(day)} 盘后复盘</title>\n<style>{STYLE}</style>\n<div class="wrap">'
            + '\n'.join(s for s in sections if s) + '</div>\n' + SCRIPT)


def document(payload: dict) -> str:
    """Complete standalone HTML document (the API and the exported file share it)."""
    body = render(payload)
    title_end = body.index('</title>') + len('</title>')
    return ('<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            + body[:title_end] + '</head><body>' + body[title_end:] + '</body></html>')


def write(directory: Path, payload: dict) -> Path:
    """Atomic write of <date>_post_close_review.html next to the markdown bundle."""
    parts = parts_of(payload)
    day = parts['strategy_lanes'].get('as_of_date') or (parts.get('recommendation_pool') or {}).get('as_of_date')
    if not day:
        raise ValueError('review page requires a settled as_of_date')
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{day}_post_close_review.html"
    text = document(payload)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='', dir=directory, delete=False, suffix='.tmp') as handle:
        temporary = Path(handle.name)
        handle.write(text)
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


# ---- sections -------------------------------------------------------------

def _masthead(day: str, run_id: str, lanes_result: dict, pool: dict) -> str:
    cov = lanes_result.get('review_coverage') or {}
    meta = [f"正式扫描 run {esc(run_id[:8])}", f"数据日期 {esc(day)} 收盘",
            f"公司研究 {cov.get('completed', 0)}/{cov.get('planned', 0)} 已落库"]
    if pool.get('decision_id'):
        meta.append(f"推荐池决策 {esc(pool['decision_id'][:8])} · {esc(pool.get('status'))}")
    else:
        meta.append('推荐池：本轮无正式决策')
    return (f'<header class="masthead"><span class="eyebrow">StockPlatform · 已结算日盘后复盘</span>'
            f'<h1>{esc(day)} 盘后复盘</h1><div class="meta-row">' + ''.join(f'<span>{m}</span>' for m in meta) + '</div></header>')


def _nav(pool: dict) -> str:
    items = [('verdict', '结论'), ('pool', '推荐池'), ('research', '候选研究'), ('lanes', '分策略结果'),
             ('market', '市场与板块'), ('news', '消息'), ('tracking', '往期跟踪'), ('system', '系统与留痕')]
    return '<nav class="toc" aria-label="目录">' + ''.join(f'<a href="#{k}">{v}</a>' for k, v in items) + '</nav>'


def _verdict(lanes_result: dict, pool: dict) -> str:
    market = lanes_result.get('market') or {}
    regime = market.get('regime') or {}
    lines = []
    recommended = pool.get('recommended') or []
    if pool.get('status') == 'ready' and pool.get('decision_id'):
        names = '、'.join(f"{esc(p.get('name'))}（优先{p.get('priority')}）" for p in recommended) or '无'
        baseline = set((pool.get('baseline') or {}).get('推荐') or [])
        downgraded = [p for p in (pool.get('reviewed') or []) if p.get('symbol') in baseline and p.get('decision') != 'recommend']
        down = '；' + '、'.join(esc(p.get('name')) for p in downgraded) + ' 从推荐降为' + '/'.join(sorted({DECISION_LABEL.get(p.get('decision'), '') for p in downgraded})) if downgraded else ''
        lines.append(f"<p><strong>推荐池已更新（决策 {esc(pool['decision_id'][:8])}，有效期至 {esc(pool.get('valid_until'))}）：推荐 {names}{down}。</strong></p>")
        if pool.get('market_assessment'):
            lines.append(f"<p>{esc(pool['market_assessment'])}</p>")
    else:
        lines.append(f"<p><strong>本轮没有正式推荐池决策。</strong>{esc(pool.get('notice') or lanes_result.get('notice') or '下面的策略名单只是扫描候选，不是推荐。')}</p>")
    if regime.get('label'):
        evidence = '；'.join(regime.get('evidence') or [])
        lines.append(f"<p>市场状态“{esc(regime['label'])}”：{esc(evidence)}。该状态只路由研究预算，不是次日上涨概率。</p>")
    return f'<section id="verdict"><div class="verdict">{"".join(lines)}</div></section>'


def _pool(pool: dict, lanes_result: dict) -> str:
    out = ['<section id="pool"><h2>推荐池决策</h2>']
    if not pool.get('decision_id'):
        out.append('<p class="meta">本轮没有发布推荐决策；扫描名单见“分策略结果”。</p></section>')
        return ''.join(out)
    for p in pool.get('recommended') or []:
        out.append(f'<article class="stock"><header><strong>{stock(p)}</strong><span class="meta">优先{p.get("priority")} · {esc(p.get("stage"))} · {esc(p.get("sector"))}</span><span class="pill key">推荐</span></header><dl>'
                   f'<dt>为什么现在</dt><dd>{esc(p.get("why_now"))}</dd>'
                   f'<dt>触发</dt><dd>{esc(p.get("trigger"))}</dd>'
                   f'<dt>失效</dt><dd>{esc(p.get("invalidation"))}</dd>'
                   f'<dt>同类比较</dt><dd>{esc(p.get("peer_comparison") or p.get("comparison"))}</dd>'
                   f'<dt>公司风险</dt><dd>{esc(p.get("company_risk"))}</dd></dl></article>')
    others = [p for p in pool.get('reviewed') or [] if p.get('decision') != 'recommend']
    if others:
        rows = ''.join(f"<tr><td>{stock(p)}</td><td><span class=\"pill {'down' if p.get('decision') == 'observe' else 'out'}\">{DECISION_LABEL.get(p.get('decision'), esc(p.get('decision')))}</span></td>"
                       f"<td>{esc(p.get('comparison'))}</td><td>{esc(p.get('invalidation'))}</td></tr>" for p in others)
        out.append('<h3>其他完成复核的股票</h3><div class="tablewrap"><table><thead><tr><th>股票</th><th>处置</th><th>比较结论</th><th>失效条件</th></tr></thead><tbody>' + rows + '</tbody></table></div>')
    groups = pool.get('target_groups') or {}
    if groups:
        out.append(f"<p class=\"meta\">前台目标：推荐 {esc('、'.join(code(s) for s in groups.get('推荐') or []) or '无')}；观察 {len(groups.get('观察') or [])} 只。{esc(pool.get('notice'))}</p>")
    out.append('</section>')
    return ''.join(out)


def _research(lanes_result: dict) -> str:
    groups = [g for g in lanes_result.get('review_groups') or [] if g.get('items') and g.get('key') != 'background']
    cov = lanes_result.get('review_coverage') or {}
    out = [f'<section id="research"><h2>候选公司研究</h2><p class="meta">{esc(cov.get("scope"))}；首位代表计划 {cov.get("planned", 0)} 只，完成 {cov.get("completed", 0)} 只。</p>']
    for g in groups:
        rows = ''.join(f"<tr><td>{stock(r)}</td><td>{esc(r.get('selection_reason'))}</td><td><strong>{esc(r.get('outcome_label'))}</strong> {esc(r.get('conclusion'))}</td><td>{esc(r.get('risk'))}</td></tr>" for r in g['items'])
        out.append(f'<h3>{esc(g.get("label"))}</h3><div class="tablewrap"><table><thead><tr><th>股票</th><th>为什么复核</th><th>结论</th><th>风险</th></tr></thead><tbody>{rows}</tbody></table></div>')
    missing = cov.get('missing_symbols') or []
    if missing:
        out.append(f"<p class=\"meta\">未完成一手复核：{esc('、'.join(missing))}；这些只是筛选候选，不升级为推荐。</p>")
    out.append('</section>')
    return ''.join(out)


def _lane_row(item: dict, state: str) -> str:
    m = item.get('metrics') or {}
    review = item.get('company_review') or {}
    concl = f'<div class="rv">研究：{esc(review.get("conclusion"))}</div>' if review.get('conclusion') else ''
    return (f"<tr><td>{stock(item)}<br><small>{esc(item.get('sector_label'))}</small></td><td>{esc(state)}</td>"
            f"<td class=\"num\">{esc(m.get('close', '—'))}<br>{pct_cell(m.get('change_pct'))}<br><small>{yi(m.get('amount'))}</small></td>"
            f"<td>{esc(item.get('reason'))}{concl}</td><td>{esc(item.get('confirmation'))}</td><td>{esc(item.get('invalidation'))}</td></tr>")


def _lanes(lanes_result: dict) -> str:
    tabs, panels = [], []
    for i, lane in enumerate(lanes_result.get('lanes') or []):
        key = esc(lane.get('key'))
        active = ' active' if i == 0 else ''
        tabs.append(f'<button class="tab{active}" role="tab" aria-selected="{"true" if i == 0 else "false"}" data-tab="{key}" id="tab-{key}">{esc(lane.get("label"))}<span class="cnt">{lane.get("total_matches", 0)}</span></button>')
        rows, seen = [], set()
        for field, label in STATE_LABEL:
            for item in lane.get(field) or []:
                if item.get('symbol') in seen:
                    continue
                seen.add(item.get('symbol'))
                rows.append(_lane_row(item, label))
        body = ('<div class="tablewrap"><table><thead><tr><th>股票</th><th>状态</th><th>收盘</th><th>为什么关注</th><th>确认条件</th><th>放弃条件</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>') if rows else f'<p class="meta">{esc(lane.get("empty_reason"))}</p>'
        route = lane.get('regime_route') or {}
        panels.append(f'<section class="panel{active}" role="tabpanel" data-panel="{key}" aria-labelledby="tab-{key}"{"" if i == 0 else " hidden"}>'
                      f'<p class="meta">{esc(lane.get("purpose"))} 匹配 {lane.get("total_matches", 0)} 只；路由 {esc(route.get("state"))}，权重 {esc(route.get("priority_weight", "—"))}。</p>{body}</section>')
    return ('<section id="lanes"><h2>九策略各自结果</h2><p class="meta">每条策略按自己的条件和排序独立展示；条件观察、结构观察、风险观察分别标注，带“研究”的行已完成公司复核。重复出现不是独立利好计票。</p>'
            '<div class="tabs" role="tablist">' + ''.join(tabs) + '</div>' + ''.join(panels) + '</section>')


def _sector_rows(rows: list[dict]) -> str:
    return ''.join(f"<tr><td>{esc(r.get('label'))}<br><small>{r.get('members')} 只</small></td><td class=\"num\">{pct_cell(r.get('change_median'))}</td>"
                   f"<td class=\"num\">{pct_cell(r.get('return10_median'))}</td><td class=\"num\">{esc(round(float(r.get('up_fraction') or 0) * 100))}%</td>"
                   f"<td class=\"num\">{r.get('limit_up', 0)}</td><td class=\"num\">{yi(r.get('flow_3d'))}</td><td>{esc(r.get('relative_strength'))}</td></tr>" for r in rows)


def _market(lanes_result: dict) -> str:
    market = lanes_result.get('market') or {}
    regime = market.get('regime') or {}
    overview = lanes_result.get('sector_overview') or {}
    sectors = [s for s in (overview.values() if isinstance(overview, dict) else overview) if isinstance(s, dict) and (s.get('members') or 0) >= 5]
    def num(row, key):
        try:
            return float(row.get(key))
        except (TypeError, ValueError):
            return 0.0
    today = sorted(sectors, key=lambda r: -num(r, 'change_median'))[:6]
    strong10 = sorted(sectors, key=lambda r: -num(r, 'return10_median'))[:6]
    weak10 = sorted(sectors, key=lambda r: num(r, 'return10_median'))[:5]
    head = '<thead><tr><th>板块</th><th>今日中位</th><th>10日中位</th><th>上涨占比</th><th>涨停</th><th>3日资金</th><th>系统标签</th></tr></thead>'
    out = ['<section id="market"><h2>市场与板块</h2>']
    up = market.get('up_fraction'); med = market.get('median_return10')
    out.append(f"<div class=\"kv\"><h3>宽度与路由</h3><ul><li>主板有效样本上涨占比 {esc(round(float(up) * 100, 1)) if up is not None else '—'}%，十日收益中位数 {pct(med)}。</li>"
               f"<li>系统状态 {esc(regime.get('label'))}，研究风险预算 {esc(round(float(regime.get('research_budget') or 0) * 100))}%；{esc('；'.join(regime.get('evidence') or []))}。</li></ul></div>")
    if sectors:
        out.append(f'<h3>今日最强方向</h3><div class="tablewrap"><table>{head}<tbody>{_sector_rows(today)}</tbody></table></div>')
        out.append(f'<h3>十日最强方向</h3><div class="tablewrap"><table>{head}<tbody>{_sector_rows(strong10)}</tbody></table></div>')
        out.append(f'<h3>十日最弱方向</h3><div class="tablewrap"><table>{head}<tbody>{_sector_rows(weak10)}</tbody></table></div>')
    out.append('<p class="meta">板块统计的是全市场成分，不是本轮候选；标签只是参考，不是评分。</p></section>')
    return ''.join(out)


def _news(lanes_result: dict) -> str:
    research = lanes_result.get('event_research') or {}
    events = research.get('events') or []
    def importance(e):
        try:
            return float(e.get('importance') or 0)
        except (TypeError, ValueError):
            return 0.0
    top = sorted(events, key=importance, reverse=True)[:6]
    out = ['<section id="news"><h2>消息与事件</h2>']
    if research.get('summary'):
        out.append(f'<p>{esc(research["summary"])}</p>')
    if top:
        out.append('<ul class="plain">' + ''.join(f"<li><strong>{esc(e.get('fact'))}</strong> {esc(e.get('transmission'))} <small>预期差：{esc(e.get('surprise'))}；{esc(e.get('horizon'))}</small></li>" for e in top) + '</ul>')
    else:
        out.append('<p class="meta">本轮没有已核验的事件；没有匹配不等于全市场没有事件。</p>')
    out.append(f'<p class="meta">消息研究只解释方向，不进入选股结论。状态：{esc(research.get("status") or "无")}。</p></section>')
    return ''.join(out)


def _tracking(lanes_result: dict) -> str:
    followup = lanes_result.get('followup') or {}
    items = [e for e in followup.get('items') or [] if e.get('display_rank') is not None and (e.get('expected_sessions') or 0) > 0]
    def window(e, h):
        w = (e.get('windows') or {}).get(str(h)) or {}
        return pct_cell(w.get('return_pct')) if w.get('status') == 'observed' else '<span>未到期</span>' if w.get('status') == 'not_due' else '<span>缺数据</span>'
    rows = ''.join(f"<tr><td>{stock(e)}</td><td>{esc(e.get('lane_label') or e.get('lane'))} / {'推荐池登记' if str(e.get('source') or '').startswith('manual') else '扫描'}</td>"
                   f"<td class=\"num\">{esc(e.get('signal_date'))} / {esc(e.get('display_rank'))}</td><td class=\"num\">{window(e, 1)}</td><td class=\"num\">{window(e, 3)}</td><td class=\"num\">{window(e, 5)}</td><td class=\"num\">{window(e, 10)}</td>"
                   f"<td>{esc(PATH_LABEL.get(e.get('path_check'), e.get('path_check')))}</td><td>{'历史补录' if e.get('timing') == 'reconstructed' else '前瞻记录'}</td></tr>" for e in items[:60])
    out = [f'<section id="tracking"><h2>往期候选跟踪</h2><p class="meta">{esc(followup.get("note"))} 账本原始记录 {followup.get("ledger_rows", "—")} 条，合并后 {followup.get("total", 0)} 条；此处只列曾在首屏展示的 {len(items)} 条（最多显示60条）。</p>']
    if rows:
        out.append('<div class="tablewrap"><table><thead><tr><th>股票</th><th>策略 / 来源</th><th>发现日 / 展示位</th><th>1日</th><th>3日</th><th>5日</th><th>10日</th><th>结构检查</th><th>证据</th></tr></thead><tbody>' + rows + '</tbody></table></div>')
    else:
        out.append('<p class="meta">尚无到期记录；不计为零收益或成功。</p>')
    out.append('</section>')
    return ''.join(out)


def _system(day: str, run_id: str, lanes_result: dict, pool: dict) -> str:
    cov = lanes_result.get('coverage') or {}
    bundle = lanes_result.get('report_bundle') or {}
    files = '、'.join(esc(r.get('filename')) for r in bundle.get('reports') or [])
    return (f'<section id="system"><h2>系统与留痕</h2><ul class="plain">'
            f'<li>扫描版本 {esc(lanes_result.get("version"))}，run {esc(run_id)}，状态 {esc(lanes_result.get("status"))}；完整历史 {cov.get("complete_history", "—")}/{cov.get("universe", "—")} 只主板股票。</li>'
            f'<li>推荐池：{esc(pool.get("status") or "无")}{"，决策 " + esc(pool.get("decision_id")) if pool.get("decision_id") else ""}；同步许可 {esc(pool.get("sync_allowed"))}。</li>'
            f'<li>同轮报告文件：{files or "—"}；接口：/api/v1/strategy/post-close/latest?as_of_date={esc(day)}。</li></ul></section>')


STYLE = """
:root{--ground:#f6f7f5;--paper:#fff;--ink:#1c2433;--ink-2:#4b5563;--ink-3:#7b8494;--line:#dde2e6;--accent:#0f6e64;--accent-soft:#e3f1ee;
--watch:#0f6e64;--watch-soft:#e3f1ee;--down:#b7791f;--down-soft:#f8efdc;--out:#b4413a;--out-soft:#f7e6e3;--up:#b4413a;--dn:#1f7a4d;--tab:#eef1ee}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--ground:#14181d;--paper:#1c2128;--ink:#e6e9ee;--ink-2:#b8c0cc;--ink-3:#8a93a1;--line:#2e353e;--accent:#5fc3b6;--accent-soft:#1c3330;
--watch:#5fc3b6;--watch-soft:#1c3330;--down:#e2b15a;--down-soft:#3a2f18;--out:#e4867f;--out-soft:#3b2220;--up:#e4867f;--dn:#6fc794;--tab:#232a32}}
:root[data-theme="dark"]{--ground:#14181d;--paper:#1c2128;--ink:#e6e9ee;--ink-2:#b8c0cc;--ink-3:#8a93a1;--line:#2e353e;--accent:#5fc3b6;--accent-soft:#1c3330;
--watch:#5fc3b6;--watch-soft:#1c3330;--down:#e2b15a;--down-soft:#3a2f18;--out:#e4867f;--out-soft:#3b2220;--up:#e4867f;--dn:#6fc794;--tab:#232a32}
body{background:var(--ground);color:var(--ink);font-family:"Noto Sans SC","PingFang SC","Microsoft YaHei",system-ui,sans-serif;font-size:15px;line-height:1.7;margin:0;padding:0 16px 48px}
.wrap{max-width:960px;margin:0 auto}
h1,h2,h3{font-family:"Noto Serif SC","Songti SC","SimSun",serif;margin:0;line-height:1.3;text-wrap:balance}
h1{font-size:30px;font-weight:700}h2{font-size:20px;font-weight:700;margin-bottom:12px;border-top:1px solid var(--line);padding-top:24px;margin-top:28px}
h3{font-size:16px;font-weight:600;margin:18px 0 6px}p{margin:0 0 10px}small,.meta{color:var(--ink-3)}
.masthead{padding:36px 0 18px;display:flex;flex-direction:column;gap:8px}
.eyebrow{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:700}
.meta-row{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:13px;color:var(--ink-3);font-variant-numeric:tabular-nums}
nav.toc{position:sticky;top:0;background:var(--ground);z-index:2;padding:10px 0;border-bottom:1px solid var(--line);display:flex;gap:6px;flex-wrap:wrap}
nav.toc a{font-size:13px;color:var(--ink-2);text-decoration:none;padding:4px 10px;border-radius:999px;background:var(--tab)}
nav.toc a:hover,nav.toc a:focus-visible{color:var(--accent);outline:2px solid var(--accent);outline-offset:1px}
.verdict{background:var(--paper);border:1px solid var(--line);border-left:4px solid var(--accent);padding:16px 18px;margin-top:18px}.verdict p:last-child{margin-bottom:0}
.pill{display:inline-block;font-size:12px;font-weight:700;padding:1px 8px;border-radius:999px;white-space:nowrap;vertical-align:middle}
.pill.watch{background:var(--watch-soft);color:var(--watch)}.pill.down{background:var(--down-soft);color:var(--down)}.pill.out{background:var(--out-soft);color:var(--out)}.pill.key{background:var(--accent);color:#fff}
.tablewrap{overflow-x:auto;margin:10px 0 6px;border:1px solid var(--line);background:var(--paper)}
table{border-collapse:collapse;width:100%;font-size:13.5px;min-width:640px}th,td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
th{font-weight:600;color:var(--ink-2);background:var(--tab);white-space:nowrap}td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.up{color:var(--up)}.dn{color:var(--dn)}
.stock{display:grid;gap:6px;background:var(--paper);border:1px solid var(--line);padding:14px 16px;margin-top:10px}
.stock header{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 12px}.stock header strong{font-size:16px}
.stock dl{display:grid;grid-template-columns:5em 1fr;gap:4px 10px;margin:0;font-size:14px}.stock dt{color:var(--ink-3)}.stock dd{margin:0}
.kv{background:var(--paper);border:1px solid var(--line);padding:12px 14px}.kv h3{margin-top:0}.kv ul{margin:0;padding-left:18px}
ul.plain{padding-left:18px;margin:0 0 10px}.note{font-size:13px;color:var(--ink-3);border-top:1px solid var(--line);padding-top:14px;margin-top:36px}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0 10px}
.tab{font:inherit;font-size:13px;padding:6px 12px;border:1px solid var(--line);background:var(--paper);color:var(--ink-2);border-radius:999px;cursor:pointer}
.tab .cnt{margin-left:6px;font-size:11px;color:var(--ink-3);font-variant-numeric:tabular-nums}
.tab.active{background:var(--accent);border-color:var(--accent);color:#fff}.tab.active .cnt{color:#e6f4f1}.tab:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.panel table{min-width:760px}.panel .rv{margin-top:4px;color:var(--accent);font-size:12.5px}
@media (max-width:520px){h1{font-size:24px}.stock dl{grid-template-columns:1fr}}
"""

SCRIPT = """<script>
(function(){
  var tabs=document.querySelectorAll('.tab'),panels=document.querySelectorAll('.panel');
  function show(key){tabs.forEach(function(t){var on=t.dataset.tab===key;t.classList.toggle('active',on);t.setAttribute('aria-selected',on?'true':'false');});
    panels.forEach(function(p){var on=p.dataset.panel===key;p.classList.toggle('active',on);p.hidden=!on;});
    try{localStorage.setItem('lane-tab',key);}catch(e){}}
  tabs.forEach(function(t){t.addEventListener('click',function(){show(t.dataset.tab);});});
  var saved=null;try{saved=localStorage.getItem('lane-tab');}catch(e){}
  if(saved&&document.querySelector('.panel[data-panel="'+saved+'"]'))show(saved);
})();
</script>
"""
