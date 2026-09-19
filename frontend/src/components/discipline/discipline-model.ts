/**
 * Pure display logic of the discipline card UI.
 *
 * Every price shown comes from the plan JSON (or the chart payload the owner
 * API derived in the generator's own basis).  This module only groups, labels,
 * clips and measures distances for display; it never derives a discipline price.
 */
import type {
  ChartBar, DailyChart, DisciplineLine, DisciplinePlan, EvaluationsResponse, Num, TradeFill,
} from './types';

export const STAGE_LABEL: Record<string, string> = {
  crash_rebound: '急跌反弹', broken: '破位', breakout_hold: '突破持有', trend_hold: '趋势持有',
  pullback_hold: '回踩持有', base_platform: '平台整理', unclassified: '未分类',
};
export const STAGE_TONE: Record<string, string> = {
  crash_rebound: 'crash', broken: 'broken', breakout_hold: 'breakout', trend_hold: 'trend',
  pullback_hold: 'pullback', base_platform: 'base', unclassified: 'neutral',
};
export const STATUS_LABEL: Record<string, string> = {
  active: '生效中', rejected_by_quality: '质量不通过', superseded: '已替代', expired: '已过期',
};
export const KIND_LABEL: Record<string, string> = {
  exposure: '仓位', hard_stop: '硬止损', soft_stop: '软止损', trail: '移动止损', time_stop: '时间止损',
  no_add: '禁加仓', take_partial: '减半仓', holiday: '休市减仓', trigger: '买入触发', cancel: '计划作废',
  chase_cap: '追高上限',
};
export const LINE_STATE_LABEL: Record<string, string> = {
  armed: '等待', triggered: '已触发', expired: '已失效', cancelled: '已失效', capped: '已越过追高上限，不买',
};
export const VERDICT_LABEL: Record<string, string> = {
  followed: '遵守', early: '提前动手', late: '迟于纪律', missed: '该做未做', against_plan: '违反纪律', unplanned: '计划外',
};
export const CHECK_LABEL: Record<string, string> = {
  has_hard_stop: '必须有硬止损线', has_time_stop: '必须有时间止损线', hard_stop_below_price: '硬止损低于参考价',
  hard_stop_single_condition: '硬止损只带单一条件', hard_stop_distance_sane: '止损距离在 ATR 与百分比合理区间',
  soft_above_hard: '软止损高于硬止损', soft_stop_separation: '软止损与硬止损、参考价各相距 ≥0.5×ATR14',
  lines_monotonic: '硬止损 < 软止损 < 参考价', every_line_evaluable: '每条线都可被系统评估',
  every_line_has_derivation: '每条线都能从 inputs 复算', exposure_line_when_over_cap: '超仓时必须有减仓线',
  holiday_line_when_closure: '长休市前必须有休市线', no_add_when_crash_or_broken: '急跌/破位必须禁加仓',
  sizing_consistent: '仓位公式可复算且不超上限', not_lowered_vs_previous: '硬止损不低于前序计划',
  valid_until_within_5_trading_days: '有效期不超过 5 个交易日', entry_reference_current: '新买入场价取计划日收盘或更晚',
};

/** Visual family of a line on the chart (colour / dash follow the spec's legend). */
export type LineStyleKey =
  'hard' | 'soft' | 'trail' | 'partial' | 'confirm' | 'trigger' | 'cap' | 'cancel' | 'reference' | 'cost';

export const LINE_STYLE: Record<LineStyleKey, { color: string; width: number; type: 'solid' | 'dashed' | 'dotted' }> = {
  hard: { color: '#d93026', width: 2.6, type: 'solid' },
  soft: { color: '#f08c00', width: 1.6, type: 'dashed' },
  trail: { color: '#1a9e5a', width: 1.6, type: 'dashed' },
  partial: { color: '#8e44ad', width: 1.5, type: 'solid' },
  confirm: { color: '#8a94a6', width: 1.4, type: 'dotted' },
  trigger: { color: '#1a9e5a', width: 2.2, type: 'solid' },
  cap: { color: '#7fd1a4', width: 1.6, type: 'dashed' },
  cancel: { color: '#6b7280', width: 1.6, type: 'solid' },
  reference: { color: '#3b82f6', width: 1, type: 'solid' },
  cost: { color: '#60a5fa', width: 1, type: 'dashed' },
};

const KIND_STYLE: Record<string, LineStyleKey> = {
  hard_stop: 'hard', soft_stop: 'soft', trail: 'trail', take_partial: 'partial', time_stop: 'confirm',
  no_add: 'confirm', trigger: 'trigger', chase_cap: 'cap', cancel: 'cancel',
};

export function num(value: Num): number | null {
  if (value === null || value === undefined || value === '') return null;
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function price2(value: Num): string {
  const parsed = num(value);
  return parsed === null ? '—' : parsed.toFixed(2);
}

export function shanghaiToday(now: Date = new Date()): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(now);
}

export function shanghaiStamp(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).formatToParts(date);
  const pick = (type: string) => parts.find((part) => part.type === type)?.value ?? '';
  return `${pick('month')}-${pick('day')} ${pick('hour')}:${pick('minute')}`;
}

export const dateOnly = (value: string | null | undefined): string => String(value ?? '').slice(0, 10);

// --------------------------------------------------------------------------
// Lines
// --------------------------------------------------------------------------
export type PriceLine = {
  id: string; index: number; kind: string; label: string; price: number; basis: 'daily' | 'minute';
  style: LineStyleKey; priority: number; line: DisciplineLine;
};

export const lineId = (line: DisciplineLine, index: number) => `${index}:${line.kind}`;

/** Every line with a price, in the plan's own order (time-only lines have no level to draw). */
export function priceLines(plan: DisciplinePlan): PriceLine[] {
  const out: PriceLine[] = [];
  plan.lines.forEach((line, index) => {
    const price = num(line.price);
    if (price === null || line.execute_by === 'time' && line.kind !== 'time_stop') return;
    out.push({
      id: lineId(line, index), index, kind: line.kind, label: line.label, price,
      basis: line.confirm?.basis === 'minute' ? 'minute' : 'daily', style: KIND_STYLE[line.kind] ?? 'confirm',
      priority: line.priority, line,
    });
  });
  return out;
}

export type MergedLine = {
  key: string; price: number; ids: string[]; kinds: string[]; labels: string[]; style: LineStyleKey; priority: number;
  text: string;
};

/** One entry per distinct price (to the cent): same-price lines become one line with several labels. */
export function mergeLines(lines: PriceLine[]): MergedLine[] {
  const groups = new Map<string, PriceLine[]>();
  for (const line of lines) {
    const key = line.price.toFixed(2);
    groups.set(key, [...(groups.get(key) ?? []), line]);
  }
  return [...groups.entries()].map(([key, members]) => {
    const ordered = [...members].sort((a, b) => a.priority - b.priority || a.index - b.index);
    const lead = ordered[0]!;
    const kinds = [...new Set(ordered.map((item) => item.kind))];
    return {
      key: `p${key}`, price: lead.price, ids: ordered.map((item) => item.id), kinds,
      labels: ordered.map((item) => item.label), style: lead.style, priority: lead.priority,
      text: `${kinds.map((kind) => KIND_LABEL[kind] ?? kind).join(' / ')} ${key}`,
    };
  }).sort((a, b) => b.price - a.price);
}

/** Short label drawn at the right end of a line, e.g. "硬止损 7.63 · 收盘跌破全部退出". */
export function lineTag(line: PriceLine | MergedLine): string {
  const kinds = 'kinds' in line ? line.kinds : [line.kind];
  const priceText = line.price.toFixed(2);
  const names = kinds.map((kind) => KIND_LABEL[kind] ?? kind).join(' / ');
  const tail: Record<string, string> = {
    hard_stop: '收盘跌破全部退出', soft_stop: '收盘跌破减半', trail: '站上后止损上移', take_partial: '天量滞涨减半',
    time_stop: '限期站回', no_add: '站回前不加仓', trigger: '收盘站上可买', chase_cap: '高于不买', cancel: '放量跌破作废',
  };
  const note = kinds.map((kind) => tail[kind]).filter(Boolean)[0];
  return note ? `${names} ${priceText} · ${note}` : `${names} ${priceText}`;
}

/**
 * Spread label positions (pixels) so no two are closer than ``gap``, keeping their order and
 * staying inside ``[top, bottom]``.  Returns the new positions in input order.
 */
export function staggerLabels(positions: number[], gap: number, top: number, bottom: number): number[] {
  const order = positions.map((value, index) => ({ value, index })).sort((a, b) => a.value - b.value);
  const placed = order.map((item) => Math.min(Math.max(item.value, top), bottom));
  for (let i = 1; i < placed.length; i += 1) placed[i] = Math.max(placed[i]!, placed[i - 1]! + gap);
  const overflow = placed.length ? placed[placed.length - 1]! - bottom : 0;
  if (overflow > 0) {
    placed[placed.length - 1] = bottom;
    for (let i = placed.length - 2; i >= 0; i -= 1) placed[i] = Math.min(placed[i]!, placed[i + 1]! - gap);
  }
  const out = new Array<number>(positions.length);
  order.forEach((item, rank) => { out[item.index] = placed[rank]!; });
  return out;
}

// --------------------------------------------------------------------------
// Axis / segments
// --------------------------------------------------------------------------
export type AxisSlot = { date: string; kind: 'bar' | 'future' | 'closed' };

/** Bars, then the validity window's future sessions, with festival closure days inserted as 休市 slots. */
export function buildAxis(chart: Pick<DailyChart, 'bars' | 'future_sessions' | 'closures'>): AxisSlot[] {
  const slots = new Map<string, AxisSlot['kind']>();
  for (const bar of chart.bars) slots.set(bar.date, 'bar');
  for (const day of chart.future_sessions) if (!slots.has(day)) slots.set(day, 'future');
  const first = chart.bars[0]?.date ?? '';
  for (const closure of chart.closures) {
    for (const day of closure.dates) if (day >= first && !slots.has(day)) slots.set(day, 'closed');
  }
  return [...slots.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([date, kind]) => ({ date, kind }));
}

/** ``[startIndex, endIndex]`` of a segment clipped to the axis; ``null`` when it does not overlap. */
export function clipSegment(axis: AxisSlot[], start: string, end: string): [number, number] | null {
  if (!axis.length || end < start) return null;
  const first = axis.findIndex((slot) => slot.date >= start);
  let last = -1;
  for (let i = axis.length - 1; i >= 0; i -= 1) if (axis[i]!.date <= end) { last = i; break; }
  if (first < 0 || last < 0 || last < first) return null;
  return [first, last];
}

/** Trading sessions strictly after the plan date, from the calendar frozen in the plan. */
export function sessionsAfterPlan(plan: DisciplinePlan, extra: string[] = []): string[] {
  const frozen = plan.metrics?.calendar?.sessions ?? [];
  return [...new Set([...frozen, ...extra])].filter((day) => day > plan.trading_date).sort();
}

/** The session a T+N time stop falls due (the N-th session after the plan date). */
export function timeStopDeadline(plan: DisciplinePlan, extraSessions: string[] = []): string | null {
  const line = plan.lines.find((item) => item.kind === 'time_stop');
  const days = line?.trading_days ?? null;
  if (!days) return null;
  return sessionsAfterPlan(plan, extraSessions)[days - 1] ?? null;
}

export function validityRemaining(plan: DisciplinePlan, today: string): number {
  const end = dateOnly(plan.valid_until);
  return sessionsAfterPlan(plan).filter((day) => day >= today && day <= end).length;
}

export function isTimeExpired(plan: DisciplinePlan, today: string): boolean {
  return dateOnly(plan.valid_until) < today;
}

export function effectiveStatus(plan: DisciplinePlan, today: string): DisciplinePlan['status'] {
  if (plan.status === 'active' && isTimeExpired(plan, today)) return 'expired';
  return plan.status;
}

// --------------------------------------------------------------------------
// Distances (display only)
// --------------------------------------------------------------------------
export type Distance = { pct: number; atr: number | null; text: string };

/** ``(price - level) / price`` and ``(price - level) / ATR14``; positive means the level is below. */
export function distance(price: number | null, level: number | null, atr14: number | null): Distance | null {
  if (price === null || level === null || price <= 0) return null;
  const pct = (price - level) / price * 100;
  const atr = atr14 && atr14 > 0 ? (price - level) / atr14 : null;
  const sign = pct >= 0 ? '' : '+';
  const pctText = `${sign}${Math.abs(pct).toFixed(1)}%`;
  return { pct, atr, text: atr === null ? pctText : `${pctText} / ${Math.abs(atr).toFixed(1)}×ATR` };
}

export function hardStopPrice(plan: DisciplinePlan): number | null {
  const fromSizing = num(plan.sizing?.hard_stop);
  if (fromSizing !== null) return fromSizing;
  const prices = plan.lines.filter((line) => line.kind === 'hard_stop').map((line) => num(line.price)).filter((value): value is number => value !== null);
  return prices.length ? Math.min(...prices) : null;
}

export function latestPrice(plan: DisciplinePlan, chart?: DailyChart | null): { price: number | null; date: string; source: string } {
  if (chart?.latest) return { price: chart.latest.close, date: chart.latest.date, source: '最新收盘' };
  const close = num(plan.metrics?.close);
  return { price: close, date: plan.trading_date, source: '生成日收盘' };
}

export function atrOf(plan: DisciplinePlan, chart?: DailyChart | null): number | null {
  return chart?.latest?.atr14 ?? num(plan.metrics?.atr14 as Num);
}

// --------------------------------------------------------------------------
// Line states from the evaluation history
// --------------------------------------------------------------------------
/** The newest state of each line, read on the line's own basis (a minute line is never judged by a daily run). */
export function latestLineStates(evaluations: EvaluationsResponse | null | undefined): Map<number, string> {
  const out = new Map<number, string>();
  for (const line of evaluations?.lines ?? []) {
    const own = line.states.filter((state) => state.basis === line.basis || state.basis === 'time');
    const last = (own.length ? own : line.states)[(own.length ? own : line.states).length - 1];
    if (last) out.set(line.index, last.state);
  }
  return out;
}

export function triggeredCount(evaluations: EvaluationsResponse | null | undefined): number {
  const states = latestLineStates(evaluations);
  return [...states.values()].filter((state) => state === 'triggered' || state === 'capped').length;
}

export function latestPlanState(evaluations: EvaluationsResponse | null | undefined): string | null {
  const changes = evaluations?.plan_state_changes ?? [];
  return changes.length ? changes[changes.length - 1]!.to : null;
}

// --------------------------------------------------------------------------
// Today's action
// --------------------------------------------------------------------------
export type TodayAction = {
  headline: string; short: string; timing: string; tone: 'danger' | 'warning' | 'success' | 'info';
  relatedKinds: string[];
};

function nextSession(plan: DisciplinePlan): string | null {
  return sessionsAfterPlan(plan)[0] ?? null;
}

const md = (day: string | null) => (day ? day.slice(5) : '下一交易日');

/**
 * The one sentence a holder acts on today, derived from the plan's own lines and the latest
 * stored evaluation.  Share counts are the plan's action values; the only arithmetic is the
 * difference "sell = held - target" the card displays.
 */
export function todayAction(plan: DisciplinePlan, evaluations?: EvaluationsResponse | null, today?: string): TodayAction {
  const failed = (plan.quality ?? []).filter((check) => !check.passed);
  if (plan.status === 'rejected_by_quality') {
    return { headline: `不可用：质量门未通过 ${failed.length} 项，不据此操作`, short: '不可用', timing: '—', tone: 'danger', relatedKinds: ['hard_stop'] };
  }
  if (today && effectiveStatus(plan, today) === 'expired') {
    return { headline: '计划已过期，等待重新生成', short: '已过期', timing: '—', tone: 'info', relatedKinds: ['hard_stop'] };
  }
  if (plan.status === 'superseded') {
    return { headline: '已被新计划替代，只读', short: '已替代', timing: '—', tone: 'info', relatedKinds: ['hard_stop'] };
  }
  const held = plan.position?.quantity ?? plan.sizing?.current_shares ?? 0;
  const states = latestLineStates(evaluations);
  const planState = latestPlanState(evaluations);
  const triggeredLine = plan.lines.findIndex((line, index) => states.get(index) === 'triggered'
    && ['exit_all'].includes(line.action.type));
  if (planState === 'exit_signalled' || triggeredLine >= 0) {
    const line = plan.lines[triggeredLine >= 0 ? triggeredLine : 0];
    return { headline: `清仓 ${held} 股（${KIND_LABEL[line?.kind ?? ''] ?? '退出线'}已触发）`, short: `清仓${held}股`,
      timing: '下一交易时段尽快执行', tone: 'danger', relatedKinds: [line?.kind ?? 'hard_stop'] };
  }
  if (plan.plan_kind === 'new_buy') {
    const trigger = plan.lines.find((line) => line.kind === 'trigger');
    const cap = plan.lines.find((line) => line.kind === 'chase_cap');
    const triggerIndex = plan.lines.findIndex((line) => line.kind === 'trigger');
    const cancelIndex = plan.lines.findIndex((line) => line.kind === 'cancel');
    if (cancelIndex >= 0 && states.get(cancelIndex) === 'triggered') {
      return { headline: '计划作废：已放量跌破作废线，不买', short: '作废', timing: '—', tone: 'info', relatedKinds: ['cancel'] };
    }
    if (triggerIndex >= 0 && states.get(triggerIndex) === 'capped') {
      return { headline: '已越过追高上限，不买', short: '追高不买', timing: '—', tone: 'warning', relatedKinds: ['trigger', 'chase_cap'] };
    }
    const shares = num(trigger?.action.value) ?? plan.sizing?.recommended_shares ?? 0;
    const capText = cap ? `、不高于 ${price2(cap.price)} ` : ' ';
    if (triggerIndex >= 0 && states.get(triggerIndex) === 'triggered') {
      return { headline: `已触发：最多买 ${shares} 股（价格不高于 ${price2(cap?.price)}）`, short: `可买${shares}股`,
        timing: '触发后下一交易日', tone: 'success', relatedKinds: ['trigger', 'chase_cap', 'hard_stop'] };
    }
    return { headline: `等待触发：收盘站上 ${price2(trigger?.price)}${capText}时最多买 ${shares} 股`, short: '等待触发',
      timing: '日线收盘确认', tone: 'info', relatedKinds: ['trigger', 'chase_cap', 'cancel', 'hard_stop'] };
  }
  const exposure = plan.lines.find((line) => line.kind === 'exposure');
  if (exposure) {
    const target = num(exposure.action.value) ?? 0;
    const sell = Math.max(0, held - target);
    // The T+1 lock of the snapshot day has lapsed by the next session: everything held is sellable then.
    const sellable = held;
    const when = md(nextSession(plan));
    if (sell > 0) {
      return target === 0
        ? { headline: `${when} 开盘15分钟内清仓 ${held} 股（可卖 ${sellable}）`, short: `清仓${held}股`,
          timing: `${when} 09:30–09:45`, tone: 'danger', relatedKinds: ['hard_stop'] }
        : { headline: `${when} 开盘15分钟内减到 ${target} 股（卖 ${sell}，可卖 ${sellable}）`, short: `减到${target}股`,
          timing: `${when} 09:30–09:45`, tone: 'warning', relatedKinds: ['hard_stop'] };
    }
  }
  const holiday = plan.lines.find((line) => line.kind === 'holiday');
  if (holiday) {
    const target = num(holiday.action.value) ?? 0;
    const day = (holiday.execute_at ?? '').slice(0, 10);
    if (held > target) {
      return { headline: `${md(day)} 收盘前减到 ${target} 股（休市前降仓）`, short: `休市前减到${target}股`,
        timing: `${md(day)} 14:55 前`, tone: 'warning', relatedKinds: ['hard_stop'] };
    }
  }
  if (planState === 'reduce_signalled') {
    return { headline: '减仓信号已触发：按减仓线执行', short: '减仓', timing: '下一交易时段', tone: 'warning', relatedKinds: ['soft_stop', 'take_partial'] };
  }
  return { headline: '持有，无需操作', short: '持有', timing: '收盘后看线', tone: 'success', relatedKinds: ['hard_stop'] };
}

/** Lines shown by default: hard stop, lines the day's action refers to, and (for a new buy) its entry lines. */
export function defaultVisibleKinds(plan: DisciplinePlan, action: TodayAction): Set<string> {
  const kinds = new Set<string>(['hard_stop', ...action.relatedKinds]);
  if (plan.plan_kind === 'new_buy') ['trigger', 'chase_cap', 'cancel'].forEach((kind) => kinds.add(kind));
  return kinds;
}

/** The nearest still-pending price line to the current price (the hard stop is shown separately). */
export function nearestPendingLine(plan: DisciplinePlan, price: number | null, evaluations?: EvaluationsResponse | null): PriceLine | null {
  if (price === null) return null;
  const states = latestLineStates(evaluations);
  const candidates = priceLines(plan).filter((line) => line.kind !== 'hard_stop' && line.basis === 'daily'
    && (!states.has(line.index) || states.get(line.index) === 'armed'));
  return candidates.sort((a, b) => Math.abs(a.price - price) - Math.abs(b.price - price))[0] ?? null;
}

// --------------------------------------------------------------------------
// Board summary
// --------------------------------------------------------------------------
/** ``Σ current_risk_pct`` of the holding plans (each = shares × stop distance / equity, frozen in the plan). */
export function totalRiskPct(plans: DisciplinePlan[]): number {
  return plans.filter((plan) => plan.plan_kind === 'holding' && plan.status !== 'superseded')
    .reduce((sum, plan) => sum + (num(plan.sizing?.current_risk_pct) ?? 0), 0);
}

/** The last session whose 15:00 close has passed at ``now``. */
export function lastClosedSession(sessions: string[], now: Date = new Date()): string | null {
  const today = shanghaiToday(now);
  const hour = Number(new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Shanghai', hour: '2-digit', hour12: false }).format(now));
  const closedToday = hour >= 15;
  const eligible = [...new Set(sessions)].filter((day) => day < today || (day === today && closedToday)).sort();
  return eligible[eligible.length - 1] ?? null;
}

function tradeInstant(trade: TradeFill): number {
  const time = trade.trade_time ? String(trade.trade_time).slice(0, 8) : '15:00:00';
  return new Date(`${trade.trade_date}T${time}+08:00`).getTime();
}

/** Why the holdings the cards were built on may no longer be the holdings: a stale snapshot or a later fill. */
export function snapshotWarnings(snapshotAt: string | null | undefined, sessions: string[], trades: TradeFill[], now: Date = new Date()): string[] {
  const out: string[] = [];
  if (!snapshotAt) return ['尚未读取到持仓快照'];
  const observed = new Date(snapshotAt).getTime();
  const lastClose = lastClosedSession(sessions, now);
  if (lastClose && observed < new Date(`${lastClose}T15:00:00+08:00`).getTime()) {
    out.push(`快照早于 ${lastClose.slice(5)} 收盘`);
  }
  const later = trades.filter((trade) => tradeInstant(trade) > observed);
  if (later.length) out.push(`快照之后有 ${later.length} 笔成交`);
  return out;
}

export function barByDate(bars: ChartBar[]): Map<string, ChartBar> {
  return new Map(bars.map((bar) => [bar.date, bar]));
}

/** Consecutive minute closes below the stop at the end of the tape (display count for the 3-bar rule). */
export function trailingBelow(closes: number[], stop: number | null): number {
  if (stop === null) return 0;
  let count = 0;
  for (let i = closes.length - 1; i >= 0; i -= 1) {
    if (closes[i]! < stop) count += 1; else break;
  }
  return count;
}
