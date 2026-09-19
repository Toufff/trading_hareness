# 交易纪律模块（trade_discipline）设计合同 v1

状态：设计已定，实施中（2026-09-18）；第二轮审查（600613 纪律卡）、其复审与第二轮 dry-run 验收（G1–G5）后于同日修订，见文末“迭代记录”。本文件是实现的唯一合同；实现与本文冲突时先改本文再改代码。

## 目的

把“触发 / 取消 / 止损 / 退出 / 时间线 / 仓位”从聊天里的自由文本，变成系统从证据推导、可机器评估、可与真实成交对账、全程落库的结构化纪律。人不手写价格；系统按模板从结构点推导，人只能接受、否决或带理由覆盖，覆盖本身也是一条不可变记录。

边界：research-only、human-decision-support-only。不连券商、不下单、不改 `personal_trade_plans` 的语义（旧表继续保留作历史）。不写 `G:\StockPlatform\current`。

## 模块位置

```
quant-service/app/trade_discipline/
  __init__.py
  contracts.py     # pydantic v2 合同：Line / Sizing / Plan / Evaluation / Compliance / Review
  stage.py         # 纯函数：日线指标 + 策略归属 → 持仓阶段(stage)
  templates.py     # 纯函数：stage → 纪律线模板（每条线带 derivation）
  generator.py     # 纯函数：inputs → DisciplinePlan（含 sizing、quality 结果）
  quality.py       # 纯函数：计划质量门（全部通过才 active，否则 rejected_by_quality 但仍落库）
  evaluator.py     # 纯函数：plan + 日线/分钟线 → 各线状态与计划状态
  reconcile.py     # 纯函数：plan + 评估 + 真实成交 → 遵守/偏离记录
  inputs.py        # 证据采集（DB 只读 + 现有行情源），产出 GenerationInputs 与 inputs_hash
  repository.py    # 落库（追加式、content_hash 幂等）与读回
  report.py        # Markdown/JSON 纪律卡（含推导表）
quant-service/app/routers/trade_discipline.py   # 只读路由
scripts/trade-discipline.py                      # CLI：generate / evaluate / reconcile / show
quant-service/migrations/versions/20260918_0105_trade_discipline.py
quant-service/tests/test_trade_discipline_*.py
```

复用而不是重写：`app/short_term_lanes/conditions.py`、`risk.py`、`rules.py`（metrics 口径：ma5/ma10 为收盘简单均值、prior_high=前5日收盘最高、recent_low=近5日收盘最低、low20=近20个交易日最低价）；`app/agent_paper/context.py` 的 `daily_bars`/`fetch_minutes`/`fetch_live_quotes`；`app/stock_workbench_indicators.py` 的 `_atr`；`app/disclosure_day_watch.next_trading_session` 或 `intraday_outcome_settlement._next_calendar_trading_date` 取交易日历；`quant.sector_membership_history`（taxonomy `longhu_ths_industry`）取板块归属；`quant.broker_portfolio_snapshots` + `quant.broker_position_snapshots` 取持仓；`quant.broker_trade_records` 取成交；`quant.recommendation_pool_decisions.result.recommended[]` 取推荐 note；最近一次 `quant.post_close_strategy_candidates` / `quant.intraday_strategy_scans` 取策略归属与 formal_state。

## 核心原则（质量门据此写成断言）

1. **均线管状态，结构点管动作。** MA5/MA10/MA20 只用于阶段判定与“软线”；硬止损、取消、触发必须来自结构点：近 N 日真实低点/收盘低点、当日低点、突破平台、急跌日低点，再加波动缓冲。
2. **硬线单条件，软线可多条件。** 硬止损只看一个可评估指标（日收盘或连续 3 分钟收盘），不附加板块/量能条件。多条件只允许出现在减仓/提醒类软线。
3. **每条线可评估。** 每条线声明 metric（daily_close / minute_close / last / low / high）、op、price 或 pct、confirm（bars、basis）、extra 条件只能取系统能计算的枚举（见下）。系统算不出的条件不许写进去。
4. **每条线可追溯。** `derivation = {rule_id, inputs: {...}, formula: "...", action_inputs: {...}, action_formula: "..."}`，任何价格都能从 inputs 复算；动作值本身是推导数（trail 的 move_stop_to 目标）时，同样能从 action_inputs 复算。模板按规则**不生成**的线也要留痕：`metrics.omitted_lines = [{kind, reason, inputs}]`，缺席的线必须能区分“规则拒绝”与“模板遗漏”。
5. **仓位取两个限制中较小者。** 风险上限 `max_shares = floor(equity × risk_per_trade_pct / (reference_price − hard_stop) / 100) × 100`（单笔 1%）；阶段上限 `cap_shares = floor(equity × cap% / reference_price / 100) × 100`，`cap%` 来自数据校准（见“单票仓位上限校准”）；`recommended_shares = min(max_shares, cap_shares)`，`sizing.binding_constraint` 记录起约束的一方。仓位调整线（exposure）按**时间**执行，不看价格。
6. **只上移不下移。** trail 线每日重算，`new = max(prev, candidate)`；任何后续计划的硬止损不得低于前一计划（除非 supersede 记录里给出 `lowered_reason`，且质量门标红）。
7. **时间线必填。** time_stop（N 个交易日无确认即退出）、valid_until、以及休市 ≥5 个自然日（劳动节/国庆/春节级别；周五休一天+周末的 3 天缺口不算）前的 holiday 线。
8. **不可变。** 计划、评估、对账、评审全部追加写；改计划 = 新计划 + `supersedes_plan_id`。`plan_key = account:symbol:trading_date:plan_kind:inputs_hash[:12]`，同证据重跑幂等，证据变化（盘中重算）即为新计划，同日也能 supersede。
9. **失败要落库。** 质量门不过的计划照样写入，status=`rejected_by_quality`，quality 字段列出失败项；不允许静默丢弃或降级成文本。

## 合同（contracts.py）

```python
CONTRACT_VERSION = "trade-discipline-v1"
LineKind = Literal["exposure","hard_stop","soft_stop","trail","time_stop","no_add",
                   "take_partial","holiday","trigger","cancel","chase_cap"]
Metric = Literal["daily_close","minute_close","last","low","high","vwap"]
Op = Literal["<","<=",">",">="]
ExtraCondition = Literal[
  "sector_change_negative",        # 所属一级行业当日涨幅 < 0（需 DB 有归属，否则不得使用）
  "sector_not_weak",               # 行业当日涨幅 >= 0
  "amount_ge_prev_day",            # 当日成交额 >= 前一日
  "volume_expand_1_5x",            # 当日成交量 >= 前5日均量 × 1.5
  "volume_contract_0_7x",          # 当日成交量 <= 前5日均量 × 0.7
  "below_vwap",                    # 最新价 < 当日 VWAP
  "after_volume_climax",           # 当日量 >= 20 日最大量 且 收盘位于当日振幅下半
]
ActionType = Literal["exit_all","reduce_to_shares","reduce_by_pct","move_stop_to","block_add","alert","buy_up_to_shares"]

class Confirm(BaseModel): bars: int = 1; basis: Literal["daily","minute"] = "daily"
class Derivation(BaseModel):
    rule_id: str; inputs: dict[str, Any]; formula: str
    action_inputs: dict[str, Any] = {}; action_formula: str = ""   # move_stop_to 目标的推导
class Action(BaseModel): type: ActionType; value: Decimal | int | None = None
class Line(BaseModel):
    kind: LineKind; label: str
    metric: Metric | None; op: Op | None; price: Decimal | None; pct: Decimal | None
    confirm: Confirm = Confirm()
    extra: list[ExtraCondition] = []
    execute_by: Literal["price","time"] = "price"     # exposure/holiday/time_stop 用 time
    execute_at: str | None = None                      # "next_open+15m" / "before_close" / "T+3_close"
    trading_days: int | None = None                    # time_stop 用
    action: Action
    derivation: Derivation
    priority: int                                      # 同时触发时的执行顺序，小者先
class Sizing(BaseModel):
    equity: Decimal; risk_per_trade_pct: Decimal; reference_price: Decimal; hard_stop: Decimal
    stop_distance: Decimal; risk_amount: Decimal; max_shares: int; target_exposure_pct: Decimal
    current_shares: int; current_exposure_pct: Decimal; recommended_shares: int
    current_risk_pct: Decimal | None      # current_shares × stop_distance / equity，披露项，不设门
class PositionRef(BaseModel):
    snapshot_id: str; observed_at: datetime; quantity: int; sellable_quantity: int
    average_cost: Decimal | None; market_price: Decimal | None; market_value: Decimal | None
class QualityCheck(BaseModel): check_id: str; passed: bool; detail: str
class DisciplinePlan(BaseModel):
    contract_version; plan_key; run_id; account_key; symbol; name
    plan_kind: Literal["holding","new_buy"]
    stage: str; template_key: str; template_version: str
    as_of_at: datetime; trading_date: date; valid_until: datetime
    position: PositionRef | None
    metrics: dict[str, Any]           # 生成时冻结的指标快照（ma5/ma10/ma20/atr14/hi20/lo20/low20/recent_low/prior_high/drawdown/...）
                                      # 另含 omitted_lines（规则拒绝的线）、calendar（休市规则读到的 closure_gaps + sessions，质量门据此重算）、
                                      # t1_locked_shares（生成日不可卖股数 = quantity − sellable_quantity，**仅当快照 observed_at 的上海日期 == trading_date**，否则为 0）
                                      # 与 t1_snapshot_at（该快照时间，卡片引用它）
    sizing: Sizing | None
    lines: list[Line]                 # 至少含 hard_stop + time_stop（holding）或 trigger + cancel + hard_stop + time_stop（new_buy）
    evidence_refs: list[str]          # snapshot_id / run_id / decision_id / bars provider+date
    quality: list[QualityCheck]; status: Literal["active","rejected_by_quality","superseded","expired"]
    supersedes_plan_id: str | None; lowered_reason: str | None
    inputs_hash: str; generator_version: str
```

评估与对账合同：

```python
class LineState(BaseModel): kind; label; state: Literal["armed","triggered","expired","cancelled","capped"]; triggered_at: datetime|None; trigger_price: Decimal|None; basis: str; evidence: dict
class Evaluation(BaseModel): plan_id; as_of_at; trading_date; basis: Literal["daily","minute"]; line_states: list[LineState]; plan_state: Literal["active","exit_signalled","reduce_signalled","expired"]; inputs_hash
class ComplianceRecord(BaseModel): plan_id; trade_record_id|None; line_kind|None; verdict: Literal["followed","early","late","missed","against_plan","unplanned"]; deviation: dict; notes: str
class Review(BaseModel): plan_id; reviewer: str; verdict: Literal["accept","override","reject"]; notes: str; overrides: dict
```

## 阶段判定（stage.py）

输入：最近 60 根日线指标 + 策略归属（最近一次盘后/盘中扫描里该股所在 lane 与 formal_state）+ 持仓成本。输出 stage 与判定依据。阈值写成模块常量，测试覆盖每个分支。

| stage | 判定（按顺序，首个命中） |
|---|---|
| `crash_rebound` | 收盘距 20 日最高 ≤ −25% 且（MA5 < MA10 或 收盘 < MA10） |
| `broken` | 收盘 < MA20 且 MA5 < MA10，且不满足上面 |
| `breakout_hold` | 昨日或今日收盘 > 前 5 日收盘最高，且 收盘 ≥ MA5 ≥ MA10 |
| `trend_hold` | MA5 ≥ MA10 ≥ MA20 且 距 20 日高回撤 > −5% |
| `pullback_hold` | MA10 ≥ MA20 且 回撤在 [−15%, −3%] 且 收盘 ≥ MA10 |
| `base_platform` | 10 日收盘区间 ≤ 8% 且 收盘在 MA20 ±3% 内 |
| `unclassified` | 其余；使用最保守模板 |

策略归属只用于 template 的微调（如 lane=pullback 时软线用 conditions.py 的同一句式），不能替代数据判定。

## 模板（templates.py）

每个 stage 一个模板函数 `build_lines(stage, metrics, position, sizing, calendar) -> list[Line]`。共性：

- `hard_stop`（必有，priority 1）：`metric=daily_close, op="<", confirm(bars=1, daily)`，价格 = `min(structure_low, reference_price × (1 − buffer), reference_price − 0.9 × ATR14, reference_price × (1 − 2%))`，四项全部以 inputs 记入 derivation（`buffer_pct`、`atr_target_multiple=0.9`、`stop_pct_target=0.02`），formula 逐项写出——即结构点只允许**下移**到最小距离，永远不许被抬到结构点之上；结构比 3×ATR14 / 12% 还远时价格保持不动，由 `hard_stop_distance_sane` 判不过、按原则 9 落库为 `rejected_by_quality`。其中 structure_low 按 stage 取：crash_rebound 用 `low20`（最近 20 个交易日的最低价，即急跌低点）——但急跌低点是一个固定点，正常反弹会离它越来越远，而 stage 判定（回撤 ≤ −25%）仍停留在 crash_rebound；因此当 `low20` 已超出止损距离上限（距参考价 > 3×ATR14 或 > 12%，与质量门同一算式）时，改用第二个认可的结构点 `min(当日低点, 昨日低点)`，`derivation.inputs.structure_source` 记 `"low20"` 或 `"two_day_low"`，`low20` 两种情形都记入 inputs，label 注明改用；两日低点也超出上限时保持不动、照常被 `hard_stop_distance_sane` 拒绝；broken/unclassified 用 `min(当日低点, 昨日低点)`；pullback 用 `recent_low`（近 5 日收盘低）；trend 用 `MA10 × (1 − 0.5%)`；breakout 用 `突破平台 prior_high × (1 − 0.5%)`；base_platform 用 10 日最低收盘。buffer = `max(1.5%, 0.6 × 日波动率%)`（与 risk.py 一致）。另给一个盘中副本 `metric=minute_close, confirm(bars=3, minute)`，同价，label 标“盘中版”。
  - **label 必须说出真正绑定的项。** `derivation.inputs.binding_term ∈ {structure, buffer, atr, pct}` 记录四个 `min` 项里实际取到最小值的那一项（并列时归 `structure`，其余三项只负责加宽），`inputs.structure_value` 记结构点本身的值。label 按它动态生成：结构点绑定时写“（急跌反弹段，结构点：最近20个交易日最低价7.92）”；被加宽时写“（急跌反弹段，结构点最近20个交易日最低价7.92距离不足最小止损距离，按 0.9×ATR14 向下加宽）”，加宽项分别写作 `0.9×ATR14` / `2%` / `波动缓冲x.xx%`。不再有按 stage 静态写死的“结构低点”字样：600613 09-18 的 7.63 是 `8.41 − 0.9×ATR14` 而非 `low20` 7.92，卡上必须能看出来。
- `soft_stop`（可选，priority 2）：价格 = MA5，允许 extra 条件，action=reduce_by_pct 50。**仅当** `hard_stop + 0.5 × ATR14 <= MA5 <= reference_price − 0.5 × ATR14` 时生成；否则不生成，并把 `{kind:"soft_stop", reason, inputs:{ma5, hard_stop, reference_price, atr14, window_low, window_high}}` 写入 `metrics.omitted_lines`。理由：软止损离现价不足半个 ATR 时下一日噪音即触发，离硬止损不足半个 ATR 时与硬止损无差别。推论：硬止损恒 ≤ 参考价 − 0.9×ATR14，窗口非空要求止损距离 ≥ 1.0×ATR14，所以 0.9×ATR 项起约束作用的计划（如 600613 09-18：距离 0.78 < 0.856）天然没有软止损——是设计使然，不是缺陷。`reason` 分两种写法：区间为空（下限 > 上限）时写“软止损区间为空（下限 8.06 > 上限 7.98，止损距离 0.78 < 1.0×ATR14 0.86），不生成”，不得写成“MA5 不在 [8.06, 7.98] 内”；区间非空但 MA5 在区间外时写“MA5 8.44 不在 [硬止损 + 0.5×ATR14, 参考价 − 0.5×ATR14] = [8.28, 8.34] 内，软止损与现价或硬止损间距不足，一日噪音即触发，故不生成”。
- `time_stop`（必有）：crash_rebound/broken 3 个交易日、其余 5 个交易日内“收盘未站回确认线（stage 相应的 MA10 或 prior_high）”则 exit_all；execute_by=time，execute_at="T+N_close"。
- `exposure`（当 `current_shares > recommended_shares` 时必有，否则不出；priority 0）：`execute_by=time, execute_at="next_open+15m"`，action=reduce_to_shares(sizing.recommended_shares)。label 同时写出两个限制及起约束的一方，例如“风险上限 1200 股（1%÷止损距离） / 阶段上限 2900 股（极端亏损5%÷该阶段主板（10%）99%两日最大跌幅18.99%=25%），取较小 1200 股”。
- `holiday`（valid_until 内存在 ≥5 自然日休市时必有；3 天的节日长周末不出线，但必须留痕：有效期内每个非普通周末（周五收盘后的周六+周日不算）且 <5 天的缺口写入 `metrics.omitted_lines` `{kind:"holiday", reason, inputs:{closed_days, last_trading_date, resume_date, threshold_days}}`）：最后交易日收盘前把仓位降到校准得到的休市上限 `holiday_exposure_pct`（休市后复开前两个交易日的 99% 最大跌幅，见“单票仓位上限校准”）；休市上限不低于阶段上限时不出休市线，理由与两个上限写入 `metrics.omitted_lines`，质量门 `holiday_line_when_closure` 按 `metrics.exposure_calibration.holiday` 同样判定。休市缺口只能由**真实开市日**起算：生成日若本身闭市，不得作为 `last_trading_date`。生成器把读到的日历冻结为 `metrics.calendar = {closure_gaps, sessions}`。
- `no_add`（crash_rebound/broken 必有；其余可选）：直到 `daily_close >= MA10`（crash_rebound）或 MA5 前 block_add。
- `trail`：`metric=daily_close, op=">=", confirm(bars=1, daily)`，触发价 arm_price = `max(anchor_price, reference_price) + 1 × ATR14`；action=move_stop_to，目标价 = `max(previous_trail, max(hard_stop, anchor_price))`，语义是“涨到 锚 + 1×ATR14 后止损上移到保本”。锚 `anchor_price`：持仓计划 = 快照平均成本（`anchor_source="average_cost"`；快照无成本时退回参考价，`"reference_price"`），新买计划 = 触发参考价（`"trigger_reference"`）。该式写入 `derivation.action_formula`、`anchor_price / anchor_source / floor_price`（及有前序时的 `previous_trail`）写入 `action_inputs`（无前序 trail 时公式不含 previous_trail 项）；只上移。label 写“日线收盘站上9.35（成本/现价孰高 + 1×ATR14）后，把止损上移到成本价8.49，只上移不下移”（新买计划写“触发参考价10.45”；前序 trail 更高时写“前序移动止损8.50（已高于成本价8.49）”）。此前的 `min(近 3 日最低, arm − 1.5×ATR)` 已废弃：600613 在 +11% 触发后止损只到 8.04、仍低于成本 8.49，锁定的是亏损，且 low3 是生成时刻的静态值。**不生成**的两种情形（原因写入 `omitted_lines`）：`target_exposure_pct == 0`（仓位线已要求清仓，移动止损无意义）；目标价 `<= hard_stop`（成本已在硬止损之下，“上移到保本”不会改变止损，零信息；reason 写“保本目标 max(硬止损 12.53, 成本价 11.90) = 12.53 不高于硬止损 12.53”）。评估器按线自带的 `metric/confirm` 判定，故 trail 与硬止损一样只在日线口径、按已收盘的日 K 收盘价确认，盘中冲高不算。
- `take_partial`：`after_volume_climax` + `below_vwap` → reduce_by_pct 50（breakout/trend/crash_rebound 用）。原文表述把 extra 条件放在前面、价格条件放在最后：“当日成交量为20日最大量且收在振幅下半且最新价跌破当日VWAP、且最新价低于8.41时，减半仓”，不写成“在 8.41 下方减半仓”——价格是最弱的一项，不是主条件。
- new_buy 计划的**入场参考价** `entry_price = max(lane.reference, 最新收盘)`（generator v3）。lane.reference 是突破平台附近的结构价，股价已远离它时（2026-09-18：冰轮环境 参考 37.94 / 收 41.52）按它定止损与股数会把真实止损距离低估一整段涨幅（冰轮 13.9%、每笔风险 1.75–2.3% 权益）。因此硬止损、止损距离、`max_shares`、移动止损锚点（`anchor_source="entry_price"`）与减半仓参考价全部以 `entry_price` 为基准（`sizing.reference_price == entry_price`）；`metrics.entry = {lane_reference, lane_reference_source, last_close, last_close_date, last_close_basis(settled|forming), entry_price, entry_source, formula}` 可复算。lane.reference 保留为结构确认线：
  - `trigger`：`daily_close >= 触发下沿` + `amount_ge_prev_day` + `sector_not_weak`，触发下沿 = `max(lane.reference, hard_stop + 0.5×ATR14)`（`derivation.formula` 与 `inputs.lane_reference / hard_stop / stop_gap_floor / binding_term` 可复算）——满足“买”的收盘绝不能同时是“退出”的收盘（09-18 冰轮环境 lane 参考 37.94 低于硬止损 38.46，下沿因此为 39.68）；买入区间 = `[触发下沿, 追高上限]`，label 写作“日线收盘在 39.68–42.74 之间…”；
  - `chase_cap`（新 kind）：`daily_close > entry_price + 0.5×ATR14` → `block_add`，“已越过追高上限，不买”；`derivation.formula = entry_price + chase_atr_multiple * atr14`，trigger 的 `derivation.inputs.price_cap` 记同一价格。评估器：trigger 被一根高于追高上限的收盘确认时，状态记 `capped`（evidence `capped_reason="已越过追高上限，不买"`），不是买入信号；
  - `cancel`：`daily_close < lane.support` + `volume_expand_1_5x`；
  - 推荐池当日该股的人读 trigger/invalidation/why_now 冻结到 `metrics.recommendation_conditions`（`note="研究条件，非系统线"`、`evaluable=false`），只作阅读，不参与任何评估。

`target_exposure_pct`（阶段上限）不再是手填表：原 crash_rebound 20 / broken 0 / breakout 25 / trend·pullback 30 / base 20 / unclassified 15 与“休市减半”没有证据，已被用户否决并删除。`risk_per_trade_pct` 默认 1.0。

### 单票仓位上限校准（`app/trade_discipline/exposure_calibration.py` + `exposure_calibration.json`）

- **用户设定**：单只股票极端亏损容忍度 = 权益的 5%；不设账户总额限制。
- **协调者设定的方法**（2026-09-19）：
  1. 极端波动 = 计划日收盘后该股接下来 2 个**实际**交易日最低价相对计划日收盘的最大跌幅 `1 − min(low[t+1], low[t+2]) / close[t]`（覆盖次日一字跌停无法卖出的情形）；统计量 = 99% 分位（同时记录 95% 分位）。
  2. 复权价：`close × adj_factor`（`research_prices.adjusted_value`，三根 bar 任一缺因子即剔除样本，不用原始价代替），除权日不会被算成暴跌；停牌行不算交易日；t 与下一交易日（或 t+1 与 t+2）相隔超过 10 个自然日即剔除。
  3. 分格 = 阶段 × 板块。阶段用生成器同一分类器（`stage.daily_metrics` + `classify_stage`）作用于 `inputs.settled_daily_bars` 会给生成器的同一 60 行原始窗口；板块按涨跌幅制度，经平台唯一的 `market_rules.a_share_limit_ratio`：主板 10% / 创业板·科创板 20% / 北交所 30% / 主板 ST 5%，ST 按当日 `limit_down / pre_close` 推断（时点正确），无跌停价时退回 `instruments.is_st`（当前值，计数记入 diagnostics）。数据 = `quant.canonical_bars_daily` 全部历史。每格至少 300 个样本，不足时用同板块全部阶段合并并标记 `fallback`。
  4. `cap% = 5 ÷ q99(%)`，向下取整到 5 的倍数，限制在 [5, 50]；对所有阶段一视同仁，**包括 broken**（不再强制清仓；破位的退出仍由硬止损、时间止损与禁加仓管理）。
  5. 休市：下一交易日之前休市 ≥5 个自然日的样本单独统计（复开后前 2 个交易日），同为阶段 × 板块、不足 300 退回同板块合并；休市上限限制在 [0, 50]；不低于阶段上限则不出休市线。
- **产物**：`quant-service/app/trade_discipline/exposure_calibration.json`（版本 `discipline-exposure-calibration-v1:<cells 摘要>`，含方法文字、容忍度、分位、期限、数据窗口、每格样本数 / q99 / q95 / cap / fallback、诊断计数）。由 `scripts/calibrate-discipline-exposure.py` 只读重算（每个连接 `default_transaction_read_only=on`，结果与输入顺序无关、可复现）。生成器只读该产物；其版本进入 `GenerationInputs.exposure_calibration_version` 与 inputs_hash，计划的 `metrics.exposure_calibration` 与 `sizing.exposure_basis` 记录所用格子。
- **2026-09-19 首次校准**（数据 2023-08-15..2026-09-18，3,416,570 个样本，26,726 个休市样本）：阶段上限 主板 crash_rebound 25 / broken 40 / breakout 30 / trend 45 / pullback 30 / base 50 / unclassified 35；创业板·科创板 15 / 30 / 30 / 40 / 30 / 50 / 30；北交所 20 / 30 / 20 / 30 / 25 / 50 / 30；主板 ST 45 / 50 / 50 / 50 / 50 / 50 / 50。休市后两日的 99% 跌幅（约 6–16%）普遍**低于**平常两日（约 9–25%），因此多数格的休市上限不低于阶段上限、不会出休市线；完整表见产物文件。

## 质量门（quality.py）

全部为纯断言，返回 `QualityCheck` 列表；任一失败则 status=`rejected_by_quality`：

- `has_hard_stop`、`has_time_stop`、`hard_stop_below_price`、`hard_stop_single_condition`（extra 为空）
- `hard_stop_distance_sane`：距离在 [0.8 × ATR14, 3 × ATR14] 且在 [1.5%, 12%]
- `soft_above_hard`、`soft_stop_separation`（存在 soft_stop 时校验 `hard_stop + 0.5×ATR14 <= soft <= reference − 0.5×ATR14`）、`lines_monotonic`（hard < soft < close；trail 触发价 > close）
- `every_line_evaluable`（metric/op/price 或 execute_by=time 三者之一完整；extra 仅取枚举；sector 条件要求 DB 有归属）
- `every_line_has_derivation`（inputs 非空、formula 非空、用 inputs 复算 price 误差 ≤ 0.01；无 price 的时间线（exposure/holiday）用 formula 复算 action.value 的股数；action=move_stop_to 的线必须带 action_formula/action_inputs 且复算 action.value 误差 ≤ 0.01）
- `exposure_line_when_over_cap`、`holiday_line_when_closure`（有效期内存在 ≥5 自然日休市时必须有 holiday 线；用模板同一个 `closure_within` 对冻结的 `metrics.calendar` 与计划自身的 `valid_until` 重新推导，**两个方向都不信** `metrics.closure_required`；没有 `metrics.calendar` 的旧行退回按 `metrics.closure.closed_days` 判定）、`no_add_when_crash_or_broken`
- `sizing_consistent`（max_shares 按公式复算相等；recommended_shares ≤ max_shares 且 ≤ target 上限）
- `not_lowered_vs_previous`（有前序 active 计划时 hard_stop 不低于其值，否则需 lowered_reason）
- `valid_until_within_5_trading_days`
- `buy_zone_valid`（仅 new_buy；持仓计划恒通过）：`hard_stop < 触发下沿 ≤ 追高上限`；区间为空或触发不高于硬止损即 `rejected_by_quality`，理由写明，绝不把触发价钳到上限。`lines_monotonic` 同时断言买入触发高于硬止损。
- `entry_reference_current`（仅 new_buy；持仓计划恒通过）：`metrics.entry` 必须存在，`last_close_date` 不早于计划交易日、证据中没有 `bars_stale_day:`（即入场价来自计划交易日的已结算收盘或更晚的盘中形成 bar），`entry_price == max(lane_reference, last_close)`，且 `sizing.reference_price == entry_price`；否则 `rejected_by_quality`。止损距离 1.5–12%、0.8–3×ATR 自然按 entry_price 衡量（`hard_stop_distance_sane` 读 `sizing.reference_price`）。

## 评估（evaluator.py）与对账（reconcile.py）

- 日线评估：每个交易日收盘后对所有 active 计划跑一次；分钟评估：可选，输入分钟序列，按 confirm.bars 连续判定。
- 状态机：`active → reduce_signalled / exit_signalled → (对账后) closed`；`valid_until` 过则 expired；time_stop 用交易日历数天。time_stop 的“站回确认线”只看**截止日当日及之前**的收盘；截止日之后才站回不撤销已触发的退出。
- 对账：成交按 symbol/日期匹配计划，verdict 规则：触发后 1 个交易日内同方向成交 = followed（一次触发按 `expected_quantity` 依次吃掉多笔同方向成交，分批止损整体算遵守，`quantity_diff` 记累计差额）；无触发或已成交满触发线要求的数量后仍卖出 = early（记录当时距各线的距离与已触发线）；触发后超过 1 个交易日才成交 = late；触发且到 valid_until 无成交 = missed；no_add 生效期间买入 = against_plan；无计划的成交 = unplanned。deviation 记录价差、时间差、数量差。

## 落库（migration 20260918_0105）

全部 `quant` schema，全部追加式：

- `discipline_generation_runs(run_id uuid pk, account_key, as_of_at timestamptz, trading_date date, generator_version, inputs_hash text, inputs jsonb, status, created_at)`
- `discipline_plans(plan_id uuid pk, run_id fk, plan_key text unique, account_key, symbol fk instruments, name, plan_kind, stage, template_key, template_version, as_of_at, trading_date, valid_until, position jsonb, metrics jsonb, sizing jsonb, lines jsonb, evidence_refs jsonb, quality jsonb, status, supersedes_plan_id uuid null, lowered_reason text null, inputs_hash, generator_version, content_hash text, created_at)`
- `discipline_evaluations(evaluation_id uuid pk, plan_id fk, as_of_at, trading_date, basis, line_states jsonb, plan_state, inputs_hash, created_at)`，unique(plan_id, as_of_at, basis)
- `discipline_compliance(compliance_id uuid pk, plan_id fk, trade_record_id uuid null, line_kind, verdict, deviation jsonb, notes, created_at)`
- `discipline_reviews(review_id uuid pk, plan_id fk, reviewer, verdict, notes, overrides jsonb, created_at)`

写入幂等：同 plan_key 且同 content_hash 返回 idempotent；不同内容抛 conflict（沿用 `ImmutableDecisionFactConflict` 语义）。索引：plans(account_key, symbol, as_of_at desc)、evaluations(plan_id, as_of_at desc)。遵守 `test_migration_contracts.py`（线性链、幂等约束）。

## 报告与 CLI

- `report.py`：为每个计划生成 Markdown 纪律卡与 JSON：头部（股票、阶段、持仓、有效期、状态），仓位表（equity、单笔风险比例与其并排的 `current_risk_pct`、风险预算、止损距离、max/recommended shares、当前持仓股数、`sellable_quantity`、仓位比例按参考价与按快照市值 `market_value / equity` 两行），仓位表下当 `metrics.t1_locked_shares > 0`（即同日快照且 `sellable < quantity`）时一句“生成日不可卖 N 股（T+1，按 MM-DD HH:MM 快照），价格线自下一交易日起可执行”，线表（kind、条件人话、价格、执行方式、优先级、rule_id、formula；“条件”列带 extra 的线先列 extra 条件、再列价格条件，如“当日成交量为20日最大量且收在振幅下半（天量滞涨）、最新价跌破当日VWAP，且最新价低于 8.41（分钟确认）”），线表后的“未生成的线及原因”（来自 `metrics.omitted_lines`），推导表（价格与动作值各自复算；无价格的时间线 exposure/holiday 复算股数并与 `action.value` 比较，“一致”列与质量门 `every_line_has_derivation` 同一容差），质量门结果，证据引用。写到 `<output_root>/<trading_date>/<symbol>-<plan_key>.md/.json`，默认 `G:/StockPlatform/reports/discipline/`。
- `inputs.py` 的日线口径：`canonical_bars_daily` 已有 `as_of` 当日已结算 bar 时，**无论几点**都直接用已结算 bar、不读实时行情（bar 存在本身证明该交易日已收盘，任何实时报价都属于更晚的交易日），`evidence_refs` 记 `bars_basis:settled`；只有当日 bar 缺席、**且 `as_of` 的上海日期就是当前交易日**（`collect(session_today=...)`，默认上海今天）才允许用 live_quote + 分钟线合成 forming bar，记 `bars_basis:settled_plus_forming` 与 `forming_bar:<source>:<date>`——回填历史 `--as-of` 永远不会把今晚的报价盖上历史日期；两者都没有记 `bars_basis:settled_only`。开市日既无当日 bar 又无 forming bar（15:00 至日线入库前、`--no-live`、行情源故障）时计划会落在上一交易日，另记 `bars_stale_day:<as_of 日>:last_settled:<最后 bar 日>`，CLI 回执 `plans[].warnings` 用白话重复一遍；闭市日（周末跑）不算 stale。
- `scripts/trade-discipline.py`：入口先 `sys.stdout/stderr.reconfigure(encoding="utf-8")`，回执里的中文名在 Windows 控制台不得变成乱码。
  - `generate --account-key citics-primary [--symbol 600613.SH ...] [--as-of ISO] [--dry-run] [--output-dir DIR] [--env-file ...]`：dry-run 只读 DB、只写文件，stdout 打印 JSON 回执（每只 symbol：stage、status、失败检查、报告路径）。非 dry-run 落库后读回校验。
  - `evaluate --date YYYY-MM-DD [--basis daily|minute]`、`reconcile --date`、`show --symbol`。
  - 与 `scripts/agent-paper-trader.py` 相同的启动方式（`load_dotenv(env_file)`、`Database()`）。
- 路由（全部 GET、只读，适配器 5680 映射到 `/api/research/discipline/...`，见 `feishu-adapter/discipline-routes.mjs`）：
  - `GET /api/v1/discipline/plans/latest?account_key=&status=&limit=`、`GET /api/v1/discipline/plans/{plan_id}`、`GET /api/v1/discipline/evaluations/latest?plan_id=&basis=`；
  - `GET /api/v1/discipline/plans/history?account_key=&symbol=`：同账户同股票全部计划（含 superseded/expired/rejected），按时间正序，附 `ladder`（每个计划的硬止损持续到下一计划交易日；`lowered` 标记下移及 `lowered_reason`）；
  - `GET /api/v1/discipline/plans/{plan_id}/chart?basis=daily|minute&date=`：daily 返回与生成器**同一口径**的日 K——`quant.canonical_bars_daily` 未复权原始价，计划交易日及之前最近 60 行（停牌行在取数后剔除）+ 之后至 `min(今天, valid_until)`，同一 `normalize_bars`，附 MA5/10/20、ATR14 序列、`sessions/future_sessions`（x 轴延伸到 valid_until）、`closures`（非普通周末的休市日，如 09-25..09-27，标“休市”）、`suspensions`、`structure_points`（low20 等结构点及其所在 K 线日期）、`hard_stop_terms`（四个 min 项的复算值与起约束项）。`price_basis` 写明口径；窗口内的除权/除息日（`pre_close ≠ 前收`）列入 `corporate_actions` 且**不复权**（生成器也不复权），若发生在计划交易日之后则 `lines_comparable=false` 并给出警告。minute 返回该交易日已入库的 longhu 1 分钟线（其他来源只计数不返回）+ VWAP；未入库且为 longhu 实时接口当前会话时读取实时分钟线；都没有时 `rows=[]` + `reason`；
  - `GET /api/v1/discipline/plans/{plan_id}/evaluations`：全部评估（两种口径），按线整理的逐日状态、`transitions`（armed→triggered 等）与 `plan_state_changes`；
  - `GET /api/v1/discipline/reconciliations?account_key=&symbol=&from=&to=`：`items` 为对账结论（六类 verdict）连同对应成交，`trades` 为窗口内全部真实成交（附其 verdicts），无数据返回空列表；默认最近 30 天，窗口 ≤ 370 天。
  - 在 `main.py` 只做 include_router，逻辑在 router / `app/trade_discipline/chart.py`（纯函数）/ `app/async_trade_discipline_read_repository.py`（只读 SELECT）。

## 验收

1. 纯函数测试：stage 每个分支；每个 stage 模板产出的线通过质量门；质量门每条断言各有一个失败样例；evaluator 对日线/分钟线各一个触发样例与一个不触发样例；reconcile 六种 verdict 各一例；sizing 公式；trail 只上移。
2. 合同测试：migration 线性链；router composition；`ruff check app tests`；`git diff --check`。
3. 用例：`generate --dry-run --symbol 600613.SH --account-key citics-primary`（真实 DB 只读）生成神奇制药的纪律卡，交由人工/上层审查；审查不过 → 改模板或质量门 → 重新生成，形成迭代记录。
4. 落库验收在发布后进行：非 dry-run 生成 → 数据库读回 → `GET /api/v1/discipline/plans/latest` 同轮一致。

## 迭代记录

- 2026-09-18 第一轮：600613/603823/000977/600664 dry-run 纪律卡，结构合格，审查列出 7 项缺陷。
- 2026-09-18 第二轮（本文已同步）：
  - F1 休市线：阈值 3→5 自然日；比例改为阶段目标的一半（broken 仍 0）。此前中秋 3 天缺口把 crash_rebound 持仓在 09-24 前清到 0 股，使整张卡其余的线失效。
  - F2 软止损：仅当 MA5 与硬止损、参考价各相距 ≥0.5×ATR14 时生成，否则记入 `omitted_lines`；新增 `soft_stop_separation` 检查；卡片增加“未生成的线及原因”。
  - F3 移动止损：目标仓位 0 或目标价不高于硬止损时不生成；`move_stop_to` 目标带 action_formula/action_inputs，质量门复算。
  - F4 披露：仓位表加 sellable_quantity、T+1 不可卖说明（`metrics.t1_locked_shares`）、`current_risk_pct`、按参考价/按快照市值两种仓位比例；均为披露，不新增会打成 rejected 的检查。
  - F5 硬止损：formula 逐项写出四个 min 项；crash_rebound 的结构点改为 `low20`（近 20 个交易日最低价）。
  - F6 CLI stdout/stderr 强制 UTF-8。
  - F7 已结算日线优先于实时合成（见“报告与 CLI”一节）。
  - 版本号：templates v2、generator v2、report v2；contract 仍为 trade-discipline-v1（只增字段，旧行可读）。（templates/report 后于 G1–G5 升到 v3。）
- 2026-09-18 第二轮复审（对 F1–F7 实现的审查，本文已同步）：
  - F5 引入的“拒绝悬崖”：600613 收盘 ≥ 9.00 时 `low20` 7.92 超出 12% 上限，一个正常反弹让 active 翻成 rejected。修法：crash_rebound 的 `low20` 超出止损距离上限时改用两日低点（见模板 hard_stop 一节），`structure_source` 留痕；两日低点也超限时仍拒绝。
  - F7 的守卫是按时间而非按证据：历史 `--as-of 14:30` 会拉今晚实时报价盖掉当日已结算 bar。修法：当日已结算 bar 存在即不读实时；`as_of` 非当前交易日亦不读实时。
  - F4 的 T+1 说明读的是最新快照而不核对快照日期，周一复用周五快照会断言一个已失效的锁。修法：仅同日快照计入 `t1_locked_shares`，卡片引用快照时间。
  - 休市规则不可审计：<5 天的缺口无痕、`closure_gaps` 未冻结、质量门只在 `closure_required=True` 时才收紧。修法：冻结 `metrics.calendar`，质量门重新推导、不信标记，节日短缺口写入 `omitted_lines`。
  - 推导表对无价格线的“一致”列打 `—`：改为按 `action.value` 复算股数，与质量门同一容差。
  - 开市日无当日 bar 又无实时 bar 时计划落在上一交易日而无提示：加 `bars_stale_day` 证据引用与 CLI 回执 warnings。
  - `HOLIDAY_EXPOSURE_PCT` 由 `TARGET_EXPOSURE_PCT / 2` 派生。
  - 软止损与 lines_monotonic 无互斥组合（复审确认），仅补充文档说明。
- 2026-09-18 第二轮 dry-run 验收（600613 主用例 aae99fb2b687，另 603823/000977/600664；本文已同步）：
  - G1 硬止损 label：四张卡都写“结构低点”，实际都是 `reference − 0.9×ATR14` 绑定（600613 low20 7.92、止损 7.63）。修法：`derivation.inputs.binding_term`（structure/buffer/atr/pct）+ `structure_value`，label 按绑定项动态生成，静态 `STRUCTURE_LABEL` 删除。
  - G2 移动止损目标价：原 `max(floor, min(low3, arm − 1.5×ATR))` 在 +11% 触发后只到 8.04、低于成本 8.49。修法：`max(hard_stop, anchor_price)`，锚 = 持仓成本 / 新买触发价，`action_inputs` 带 `anchor_source`；不高于硬止损时仍省略。
  - G3 trail 触发条件：`metric=last` 配日线确认是口径混合。修法：`metric=daily_close, confirm(1, daily)`，评估器按线声明判定，盘中冲高不触发。
  - G4 软止损省略文案：区间为空时不再打印“MA5 不在 [8.06, 7.98] 内”，改为“软止损区间为空（下限 8.06 > 上限 7.98，止损距离 0.78 < 1.0×ATR14 0.86），不生成”；区间非空、MA5 在外时保留原文案。
  - G5 减半仓原文表述：extra 条件在前、价格条件在后；卡片“条件”列同样先 extra 后价格。
  - 版本号：templates v3、report v3；generator 仍 v2、contract 仍 trade-discipline-v1（只增 inputs 字段，旧行可读；旧行 trail 的 `metric=last` 评估器仍按 close 读取）。

- 2026-09-19 第三轮（新买入场价 + 纪律卡界面）：
  - H1 新买入场价：`entry_price = max(lane.reference, 最新收盘)`，止损/距离/股数/移动止损/减半仓按它重算；新增 `chase_cap` 线与评估状态 `capped`；新增质量检查 `entry_reference_current`；推荐池人读条件冻结为 `metrics.recommendation_conditions`（研究条件，非系统线）。用 09-18 三只真实日线回归：彤程新材 73.55/70.37（4.32%，300 股，风险 0.96%）、大族激光 99.60/94.83（4.79%，200 股，0.96%）、冰轮环境 41.52/38.46（7.37%，300 股，0.93%）。
  - H1b 复审：冰轮环境买入触发 37.94 低于硬止损 38.46（收盘 38.0 同时满足“买”与“低于止损”）。触发下沿改为 `max(lane.reference, hard_stop + 0.5×ATR14)`，新增 `buy_zone_valid`；三只真实区间：彤程新材 [72.13, 75.31]、大族激光 [97.48, 102.25]、冰轮环境 [39.68, 42.74]，均由止损间隔项起约束。
  - H2 `GenerationInputs.generator_version` 进入 inputs_hash：同一证据换生成器版本得到新的 plan_key（supersede），而不是与旧版本同 key 不同内容的冲突。
  - H3 只读接口：history / chart / evaluations / reconciliations（见“报告与 CLI”路由一节）；前端“我的持仓”纪律卡与推荐池新买纪律卡（`frontend/src/components/discipline/`）。
  - 版本号：generator v3、templates v4、report v4；contract 仍为 trade-discipline-v1（只增 kind/state/字段，旧行可读；旧 new_buy 行没有 `metrics.entry`，重新质检会判 `entry_reference_current` 不通过——这正是缺陷本身）。

- 2026-09-19 第四轮（仓位上限校准）：删除手填 `TARGET_EXPOSURE_PCT` / `HOLIDAY_EXPOSURE_PCT`，改为数据校准（见“单票仓位上限校准”）；`recommended_shares = min(风险上限, 阶段上限)`，exposure 线在 `current_shares > recommended_shares` 时出现并写明起约束的限制；broken 不再强制 0。版本：generator v4、templates v5、report v5。

## 后续

agent_paper 上下文读取 discipline_plans；盘中分钟评估接入 `INTRADAY_ALERTING` 推送。
