"""Read-only business coverage matrix. HTTP success is never a blanket pass."""
from datetime import datetime, timezone

INDICES=('000001.SH','399001.SZ','399006.SZ','000688.SH','000300.SH','000905.SH','000852.SH')


def assess(facts):
    checks=[]
    def add(key,label,passed,evidence,reason):
        checks.append(dict(key=key,label=label,status='passed' if passed else 'attention',evidence=evidence,reason=reason))
    market=facts.get('market') or {}
    add('equities','沪深个股日线日期与规模',market.get('stocks',0)>0 and market.get('baseline_stocks',0)>0
        and market.get('stocks',0)>=market.get('baseline_stocks',0)*.95
        and market.get('date')==facts.get('expected_date'),
        {**market,'expected_date':facts.get('expected_date'),'minimum_recent_coverage_ratio':.95},
        '独立个股日期及前五个记录日最大覆盖的95%；不代表每个标的字段均完整，缺口由后续跟踪单列')
    indices=facts.get('indices') or []
    add('indices','主要指数成交额',len(indices)==len(INDICES) and all(r.get('amount') is not None for r in indices),
        indices,'七个指数分别检查，指数点位不与成分股成交量相乘')
    scan=facts.get('scan') or {}
    add('scan','同轮九策略',scan.get('status')=='completed' and scan.get('lane_count')==9 and str(scan.get('as_of_date'))==str(market.get('date')),
        scan,'有文件不等于日期正确或九策略齐全')
    coverage=facts.get('discipline') or {}
    parts=coverage.get('coverage',{})
    add('discipline','纪律提醒覆盖',bool(parts) and not coverage.get('gaps'),coverage,
        '无效计划不会绕过质量检查；持仓和正式推荐分别给出分母')
    delivery=facts.get('delivery') or {}
    add('delivery','实际推送回执',delivery.get('sent',0)>0 and delivery.get('failed',0)==0,
        delivery,'只检查实际历史发送回执，不发送测试消息；不证明手机通知设置')
    tracking=facts.get('tracking') or {}
    add('tracking','后续价格评价',tracking.get('evaluated',0)>0 and tracking.get('gaps',0)==0,tracking,
        '逐股票缺值与复权缺口独立显示；旧候选不补造前瞻买点')
    governance=facts.get('governance') or {}
    add('governance','改善事项推进',bool(governance) and governance.get('unrouted',0)==0
        and governance.get('blocked',0)==0 and governance.get('stale_pre_experiment',0)==0,governance,
        '有下一步标签不等于已推进；阻断及超过24小时仍在实验前的事项单列，不能自动激活')
    news=facts.get('news') or {}
    add('news','消息研究时点',news.get('status')=='analyzed' and news.get('age_hours',999)<24,news,
        '消息关联回执不等于已经改变评分；更正与失效必须留痕')
    errors=facts.get('errors',[])
    for error in errors:
        add(error['section']+'_read','核查数据读取',False,error,'读取失败不能解释为没有问题')
    return {'status':'passed' if all(c['status']=='passed' for c in checks) else 'partial',
        'checks':checks,'checked_at':facts.get('checked_at'),'live_effect':'none',
        'profitability_verified':False,'future_reliability_guaranteed':False}


def collect(database, account_key='citics-primary', as_of=None):
    now=as_of or datetime.now(timezone.utc)
    facts={'checked_at':now.isoformat(),'errors':[]}
    def query(section, action):
        try:
            with database.transaction(statement_timeout_ms=15000) as c:
                c.execute('SET TRANSACTION READ ONLY')
                facts[section]=action(c)
        except Exception as exc:
            facts['errors'].append({'section':section,'error_type':type(exc).__name__})
    from zoneinfo import ZoneInfo
    from datetime import time, timedelta
    local=now.astimezone(ZoneInfo('Asia/Shanghai'))
    through=local.date() if local.time()>=time(16) else local.date()-timedelta(days=1)
    query('expected_date',lambda c: c.execute("SELECT max(calendar_date) AS d FROM quant.market_trade_calendar WHERE exchange='SSE' AND is_open AND calendar_date<=%s",(through,)).fetchone()['d'])
    query('market', lambda c: dict(c.execute("""SELECT max(trading_date) AS date,
      count(*) FILTER (WHERE trading_date=(SELECT max(trading_date) FROM quant.canonical_bars_daily
        WHERE symbol ~ '^(60|68|00|30)[0-9]{4}\\.(SH|SZ)$' AND symbol NOT LIKE '000%%.SH'))::int AS stocks
      FROM quant.canonical_bars_daily WHERE symbol ~ '^(60|68|00|30)[0-9]{4}\\.(SH|SZ)$' AND symbol NOT LIKE '000%%.SH'""").fetchone()))
    query('baseline_stocks',lambda c:c.execute("""SELECT max(n) AS n FROM
      (SELECT count(*) AS n FROM quant.canonical_bars_daily WHERE trading_date<%s
       AND symbol ~ '^(60|68|00|30)[0-9]{4}\\.(SH|SZ)$' AND symbol NOT LIKE '000%%.SH'
       GROUP BY trading_date ORDER BY trading_date DESC LIMIT 5) previous""",
      ((facts.get('market') or {}).get('date'),)).fetchone()['n'])
    if 'market' in facts: facts['market']['baseline_stocks']=facts.get('baseline_stocks') or 0
    query('indices', lambda c: [dict(r) for r in c.execute('SELECT symbol,trading_date,amount FROM quant.canonical_bars_daily WHERE trading_date=%s AND symbol=ANY(%s) ORDER BY symbol',
        ((facts.get('market') or {}).get('date'),list(INDICES))).fetchall()])
    query('scan', lambda c: dict(c.execute("""SELECT run_id,as_of_date,status,
      jsonb_array_length(summary#>'{strategy_lanes,lanes}') AS lane_count,
      summary#>'{strategy_lanes,review_coverage}' AS research_coverage
      FROM quant.post_close_strategy_runs WHERE summary ? 'strategy_lanes'
      ORDER BY as_of_date DESC,updated_at DESC LIMIT 1""").fetchone() or {}))
    def scope(c):
        from .trade_discipline.alerts_eligibility import load_alert_scope
        value=load_alert_scope(c,account_key=account_key,as_of=now)
        return {'coverage':value.coverage,'gaps':value.has_coverage_gaps,'excluded':value.excluded,
                'blockers':value.blockers,'scope_fingerprint':value.fingerprint}
    query('discipline',scope)
    query('delivery', lambda c: dict(c.execute("""SELECT count(*) FILTER (WHERE status='sent')::int AS sent,
      count(*) FILTER (WHERE status='failed')::int AS failed,max(updated_at) AS latest
      FROM quant.intraday_advisory_deliveries WHERE created_at >= %s::date""",
      ((facts.get('market') or {}).get('date'),)).fetchone()))
    query('tracking', lambda c: dict(c.execute("""WITH latest AS (SELECT DISTINCT ON (origin_id) evidence
      FROM quant.strategy_observation_evaluations ORDER BY origin_id,as_of_date DESC,recorded_at DESC)
      SELECT count(*)::int AS evaluated,count(*) FILTER (WHERE evidence->>'status' IN ('data_gap','adjustment_gap','calendar_gap'))::int AS gaps,
      count(*) FILTER (WHERE evidence#>>'{virtual_entry,contract,status}'='frozen')::int AS frozen_virtual_contracts FROM latest""").fetchone()))
    query('governance', lambda c: dict(c.execute("""SELECT count(*)::int AS total,
      count(*) FILTER (WHERE state IN ('experiment','observing','validated','ready','activated'))::int AS experiment_or_later,
      count(*) FILTER (WHERE state IN ('discovered','reviewed','proposed','designed')
        AND i.updated_at < %s - interval '24 hours')::int AS stale_pre_experiment,
      count(*) FILTER (WHERE state NOT IN ('activated','rejected') AND
        (SELECT d.evidence#>>'{work_plan,blocker}' FROM quant.strategy_governance_diagnostics d
         WHERE d.item_id=i.id AND jsonb_typeof(d.evidence->'work_plan')='object'
         ORDER BY d.recorded_at DESC LIMIT 1) IS NOT NULL)::int AS blocked,
      count(*) FILTER (WHERE state NOT IN ('activated','rejected') AND NOT EXISTS
        (SELECT 1 FROM quant.strategy_governance_diagnostics d WHERE d.item_id=i.id AND d.evidence ? 'work_plan'
         AND d.evidence->'work_plan' IS NOT NULL AND d.evidence->'work_plan' != 'null'::jsonb))::int AS unrouted
      FROM quant.strategy_governance_items i""",(now,)).fetchone()))
    query('news',lambda c: dict(c.execute('SELECT status,cutoff,extract(epoch FROM (%s-cutoff))/3600 AS age_hours FROM quant.event_research_runs WHERE cutoff<=%s ORDER BY cutoff DESC LIMIT 1',(now,now)).fetchone() or {}))
    return assess(facts)
