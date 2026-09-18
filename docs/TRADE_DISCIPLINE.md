# 交易纪律模块（trade_discipline）设计合同 v1

状态：设计已定，实施中（2026-09-18）。本文件是实现的唯一合同；实现与本文冲突时先改本文再改代码。

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

复用而不是重写：`app/short_term_lanes/conditions.py`、`risk.py`、`rules.py`（metrics 口径：ma5/ma10 为收盘简单均值、prior_high=前5日收盘最高、recent_low=近5日收盘最低）；`app/agent_paper/context.py` 的 `daily_bars`/`fetch_minutes`/`fetch_live_quotes`；`app/stock_workbench_indicators.py` 的 `_atr`；`app/disclosure_day_watch.next_trading_session` 或 `intraday_outcome_settlement._next_calendar_trading_date` 取交易日历；`quant.sector_membership_history`（taxonomy `longhu_ths_industry`）取板块归属；`quant.broker_portfolio_snapshots` + `quant.broker_position_snapshots` 取持仓；`quant.broker_trade_records` 取成交；`quant.recommendation_pool_decisions.result.recommended[]` 取推荐 note；最近一次 `quant.post_close_strategy_candidates` / `quant.intraday_strategy_scans` 取策略归属与 formal_state。

## 核心原则（质量门据此写成断言）

1. **均线管状态，结构点管动作。** MA5/MA10/MA20 只用于阶段判定与“软线”；硬止损、取消、触发必须来自结构点：近 N 日真实低点/收盘低点、当日低点、突破平台、急跌日低点，再加波动缓冲。
2. **硬线单条件，软线可多条件。** 硬止损只看一个可评估指标（日收盘或连续 3 分钟收盘），不附加板块/量能条件。多条件只允许出现在减仓/提醒类软线。
3. **每条线可评估。** 每条线声明 metric（daily_close / minute_close / last / low / high）、op、price 或 pct、confirm（bars、basis）、extra 条件只能取系统能计算的枚举（见下）。系统算不出的条件不许写进去。
4. **每条线可追溯。** `derivation = {rule_id, inputs: {...}, formula: "..."}`，任何价格都能从 inputs 复算。
5. **仓位由止损距离反推。** `max_shares = floor(equity × risk_per_trade_pct / (reference_price − hard_stop) / 100) × 100`；同时受 stage 的 `target_exposure_pct` 上限约束。仓位调整线（exposure）按**时间**执行，不看价格。
6. **只上移不下移。** trail 线每日重算，`new = max(prev, candidate)`；任何后续计划的硬止损不得低于前一计划（除非 supersede 记录里给出 `lowered_reason`，且质量门标红）。
7. **时间线必填。** time_stop（N 个交易日无确认即退出）、valid_until、以及休市 ≥3 个自然日前的 holiday 线。
8. **不可变。** 计划、评估、对账、评审全部追加写；改计划 = 新计划 + `supersedes_plan_id`。
9. **失败要落库。** 质量门不过的计划照样写入，status=`rejected_by_quality`，quality 字段列出失败项；不允许静默丢弃或降级成文本。

## 合同（contracts.py）

```python
CONTRACT_VERSION = "trade-discipline-v1"
LineKind = Literal["exposure","hard_stop","soft_stop","trail","time_stop","no_add",
                   "take_partial","holiday","trigger","cancel"]
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
class Derivation(BaseModel): rule_id: str; inputs: dict[str, Any]; formula: str
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
    metrics: dict[str, Any]           # 生成时冻结的指标快照（ma5/ma10/ma20/atr14/hi20/lo20/recent_low/prior_high/drawdown/...）
    sizing: Sizing | None
    lines: list[Line]                 # 至少含 hard_stop + time_stop（holding）或 trigger + cancel + hard_stop + time_stop（new_buy）
    evidence_refs: list[str]          # snapshot_id / run_id / decision_id / bars provider+date
    quality: list[QualityCheck]; status: Literal["active","rejected_by_quality","superseded","expired"]
    supersedes_plan_id: str | None; lowered_reason: str | None
    inputs_hash: str; generator_version: str
```

评估与对账合同：

```python
class LineState(BaseModel): kind; label; state: Literal["armed","triggered","expired","cancelled"]; triggered_at: datetime|None; trigger_price: Decimal|None; basis: str; evidence: dict
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

- `hard_stop`（必有，priority 1）：`metric=daily_close, op="<", confirm(bars=1, daily)`，价格 = `min(structure_low, reference_price × (1 − buffer))`，其中 structure_low 按 stage 取：crash_rebound/broken 用 `min(当日低点, 昨日低点)`；pullback 用 `recent_low`（近 5 日收盘低）；trend 用 `MA10 × (1 − 0.5%)`；breakout 用 `突破平台 prior_high × (1 − 0.5%)`；base_platform 用 10 日最低收盘。buffer = `max(1.5%, 0.6 × 日波动率%)`（与 risk.py 一致）。另给一个盘中副本 `metric=minute_close, confirm(bars=3, minute)`，同价，label 标“盘中版”。
- `soft_stop`（可选，priority 2）：MA5 或 lane 参考线，允许 extra 条件，action=reduce_by_pct 50。
- `time_stop`（必有）：crash_rebound/broken 3 个交易日、其余 5 个交易日内“收盘未站回确认线（stage 相应的 MA10 或 prior_high）”则 exit_all；execute_by=time，execute_at="T+N_close"。
- `exposure`（当 current_exposure_pct > target_exposure_pct 时必有，priority 0）：`execute_by=time, execute_at="next_open+15m"`，action=reduce_to_shares(sizing.recommended_shares)。
- `holiday`（valid_until 内存在 ≥3 自然日休市时必有）：最后交易日收盘前把仓位降到 `holiday_exposure_pct`（crash_rebound/broken 0%，其余 target 的一半）。
- `no_add`（crash_rebound/broken 必有；其余可选）：直到 `daily_close >= MA10`（crash_rebound）或 MA5 前 block_add。
- `trail`：当 `last >= reference_price + 1 × ATR14` 后启用，价格 = `max(prev, min(近 3 日最低, last − 1.5 × ATR14))`，action=move_stop_to；只上移。
- `take_partial`：`after_volume_climax` + `below_vwap` → reduce_by_pct 50（breakout/trend/crash_rebound 用）。
- new_buy 计划另有 `trigger`（`daily_close >= reference` + `amount_ge_prev_day` + `sector_not_weak`）与 `cancel`（`daily_close < cancel_price` + `volume_expand_1_5x`），reference/cancel 直接取 lane 的 reference/support。

`target_exposure_pct`：crash_rebound 20、broken 0（即 exposure 线 = 清仓）、breakout_hold 25、trend_hold 30、pullback_hold 30、base_platform 20、unclassified 15。`risk_per_trade_pct` 默认 1.0。

## 质量门（quality.py）

全部为纯断言，返回 `QualityCheck` 列表；任一失败则 status=`rejected_by_quality`：

- `has_hard_stop`、`has_time_stop`、`hard_stop_below_price`、`hard_stop_single_condition`（extra 为空）
- `hard_stop_distance_sane`：距离在 [0.8 × ATR14, 3 × ATR14] 且在 [1.5%, 12%]
- `soft_above_hard`、`lines_monotonic`（hard < soft < close；trail 触发价 > close）
- `every_line_evaluable`（metric/op/price 或 execute_by=time 三者之一完整；extra 仅取枚举；sector 条件要求 DB 有归属）
- `every_line_has_derivation`（inputs 非空、formula 非空、用 inputs 复算 price 误差 ≤ 0.01）
- `exposure_line_when_over_cap`、`holiday_line_when_closure`、`no_add_when_crash_or_broken`
- `sizing_consistent`（max_shares 按公式复算相等；recommended_shares ≤ max_shares 且 ≤ target 上限）
- `not_lowered_vs_previous`（有前序 active 计划时 hard_stop 不低于其值，否则需 lowered_reason）
- `valid_until_within_5_trading_days`

## 评估（evaluator.py）与对账（reconcile.py）

- 日线评估：每个交易日收盘后对所有 active 计划跑一次；分钟评估：可选，输入分钟序列，按 confirm.bars 连续判定。
- 状态机：`active → reduce_signalled / exit_signalled → (对账后) closed`；`valid_until` 过则 expired；time_stop 用交易日历数天。
- 对账：成交按 symbol/日期匹配计划，verdict 规则：触发后 1 个交易日内同方向成交 = followed；无触发但卖出 = early（记录当时距各线的距离）；触发后超过 1 个交易日才成交 = late；触发且到 valid_until 无成交 = missed；no_add 生效期间买入 = against_plan；无计划的成交 = unplanned。deviation 记录价差、时间差、数量差。

## 落库（migration 20260918_0105）

全部 `quant` schema，全部追加式：

- `discipline_generation_runs(run_id uuid pk, account_key, as_of_at timestamptz, trading_date date, generator_version, inputs_hash text, inputs jsonb, status, created_at)`
- `discipline_plans(plan_id uuid pk, run_id fk, plan_key text unique, account_key, symbol fk instruments, name, plan_kind, stage, template_key, template_version, as_of_at, trading_date, valid_until, position jsonb, metrics jsonb, sizing jsonb, lines jsonb, evidence_refs jsonb, quality jsonb, status, supersedes_plan_id uuid null, lowered_reason text null, inputs_hash, generator_version, content_hash text, created_at)`
- `discipline_evaluations(evaluation_id uuid pk, plan_id fk, as_of_at, trading_date, basis, line_states jsonb, plan_state, inputs_hash, created_at)`，unique(plan_id, as_of_at, basis)
- `discipline_compliance(compliance_id uuid pk, plan_id fk, trade_record_id uuid null, line_kind, verdict, deviation jsonb, notes, created_at)`
- `discipline_reviews(review_id uuid pk, plan_id fk, reviewer, verdict, notes, overrides jsonb, created_at)`

写入幂等：同 plan_key 且同 content_hash 返回 idempotent；不同内容抛 conflict（沿用 `ImmutableDecisionFactConflict` 语义）。索引：plans(account_key, symbol, as_of_at desc)、evaluations(plan_id, as_of_at desc)。遵守 `test_migration_contracts.py`（线性链、幂等约束）。

## 报告与 CLI

- `report.py`：为每个计划生成 Markdown 纪律卡与 JSON：头部（股票、阶段、持仓、有效期、状态），仓位表（equity、风险预算、止损距离、max/recommended shares），线表（kind、条件人话、价格、执行方式、优先级、rule_id、formula），质量门结果，证据引用。写到 `<output_root>/<trading_date>/<symbol>-<plan_key>.md/.json`，默认 `G:/StockPlatform/reports/discipline/`。
- `scripts/trade-discipline.py`：
  - `generate --account-key citics-primary [--symbol 600613.SH ...] [--as-of ISO] [--dry-run] [--output-dir DIR] [--env-file ...]`：dry-run 只读 DB、只写文件，stdout 打印 JSON 回执（每只 symbol：stage、status、失败检查、报告路径）。非 dry-run 落库后读回校验。
  - `evaluate --date YYYY-MM-DD [--basis daily|minute]`、`reconcile --date`、`show --symbol`。
  - 与 `scripts/agent-paper-trader.py` 相同的启动方式（`load_dotenv(env_file)`、`Database()`）。
- 路由：`GET /api/v1/discipline/plans/latest?account_key=`、`GET /api/v1/discipline/plans/{plan_id}`、`GET /api/v1/discipline/evaluations/latest?plan_id=`；在 `main.py` 只做 include_router，逻辑在 router/repository。

## 验收

1. 纯函数测试：stage 每个分支；每个 stage 模板产出的线通过质量门；质量门每条断言各有一个失败样例；evaluator 对日线/分钟线各一个触发样例与一个不触发样例；reconcile 六种 verdict 各一例；sizing 公式；trail 只上移。
2. 合同测试：migration 线性链；router composition；`ruff check app tests`；`git diff --check`。
3. 用例：`generate --dry-run --symbol 600613.SH --account-key citics-primary`（真实 DB 只读）生成神奇制药的纪律卡，交由人工/上层审查；审查不过 → 改模板或质量门 → 重新生成，形成迭代记录。
4. 落库验收在发布后进行：非 dry-run 生成 → 数据库读回 → `GET /api/v1/discipline/plans/latest` 同轮一致。

## 后续（不在 v1）

agent_paper 上下文读取 discipline_plans；复盘页把评估时间线与真实成交叠加；前端“我的持仓”展示纪律卡；盘中分钟评估接入 `INTRADAY_ALERTING` 推送。
