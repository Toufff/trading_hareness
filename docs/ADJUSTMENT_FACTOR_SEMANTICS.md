# 复权因子语义与修复手册（2026-09-19，因子车道已完全去掉 tushare）

本文件是 `adj_factor` 语义的权威说明。**第 0 节是现行做法**：因子只来自 longhu（开盘啦授权
接口），修复与回补用 `scripts/adjustment-factor-maintenance.py repair`。第 3、4 节是
release 2 时代基于 tushare 原始记录的一次性修复 SQL，**已被第 0 节取代，不要再执行**，
保留只为审计链。**本分支没有对生产库做任何写入**；第 0.5 节的数字全部来自只读连接
（`default_transaction_read_only=on`）。

---

## 0. 现行：因子从 longhu 推导（用户决定："完全去掉 tushare，用 longhu 来处理"）

### 0.1 范围

- **不再调用 tushare 的**：`app/adjustment_factor_maintenance.py`（`sync` / `post_close_sync` /
  `repair` / `validate`）、盘后非门控阶段 `adjustment_factors`、04:30 计划任务
  `trading-hareness-adjustment-factors`、一次性修复。`AdjustmentFactorMaintenanceDependencies`
  里没有任何 tushare 字段（`tests/test_longhu_adjustment_factors.py::NoTushareOnTheFactorLaneTests`
  钉住）。owner `runtime.env` 自 2026-09-03 起就没有 `TUSHARE_*`，旧车道每晚都以
  `no configured provider supports adj_factor` 失败——这就是改造的起因。
- **没有删的**：仓库里其它 tushare 路线（资金流、涨跌停列表、目录等）。peer 跑同一套代码，
  可能仍在用。仍然触及因子/日控制面的 tushare 依赖见 0.7。
- **库里已有的 tushare 因子**只被**读**：作为每只票的锚点和窗口内的校验点，绝不被推导值覆盖。

### 0.2 证据（一次调用，`GetKLineDay_W14`）

实测（2026-09-19，只读）：

1. `y[i][1]` 是**前复权（qfq）收盘**，以最新一根为基准。`Is_FS` 参数被忽略（0/1/2/不传
   返回同一条序列），`app/longhu_vendor_source.parse_daily_kline_payload` 注释里的
   "unadjusted" 与事实不符（见 0.8 风险）。复权方式是**减法**：`qfq = (raw - 每股现金) / (1 + 每股送转)`，
   不是乘法；所以同一除权段内 `qfq/raw` 会随价格漂移，"比值之比"在普通交易日也会出现
   `股息率 × 当日涨跌幅` 量级的假台阶。qfq 只做交叉校验，不做主信号。
2. `CQ[i]` 是**厂商自己的除权记录**，落在除权日：`"每10股送转,f1,f2,每10股派现"`。配合前一日
   原始收盘即可复现交易所除权参考价（四舍五入到 0.01）：001388.SZ 07-17 `4.8,0,0,5` → 22.49，
   688399.SH 07-10 `4.8,0,0,0` → 30.22，603155.SH 07-16 `4.5,0,0,3.5` → 16.83，均与公布的
   pre_close 一致。`f1/f2`（配股字段）在样本里只出现一次（300176.SZ `0,4,33.6,0`），语义未验证，
   带 `rights_fields_unverified` 标记。
3. bar 自己的 `pre_close`：tushare 时代是交易所公布值；longhu 时代是**同一份 qfq 当晚的前一根**，
   即除权参考价取整到分。两种情况下 `前一日原始收盘 / pre_close` 都等于 tushare 的台阶——
   但厂商的取整与交易所不同：半分时厂商向下（600517.SH 09-17：4.98-0.045=4.935，厂商 4.93，
   交易所 4.94），所以厂商 pre_close 与 CQ 参考价相差 1 分以内时取 CQ 参考价（四舍五入，交易所规则）。
4. **补抓的 longhu bar 是前复权价**：longhu bar 的 close/pre_close 是**抓取当天**的 qfq 序列。
   当晚抓的最新一根等于原始价；但若某个交易日是在其后的除权日**之后**才补抓，其间每一次除权都已被
   减掉。生产实例：09-07、09-08、09-09 的 longhu bar（约 5,000 根/日）都是 09-10 12:09~12:38 才落库
   （`available_at` = 09-10），002073.SZ 09-07 的 close/pre_close 是 5.83/5.83，而 09-04 原始收盘 5.85——
   0.02 的派息 09-09 才除权。

### 0.3 规则（`app/longhu_adjustment_factors.decide_step`，逐对相邻 bar）

累计因子沿用 tushare 口径：`factor_d = factor_{d-1} × step_d`。

| 条件 | step | basis |
|---|---|---|
| 有 CQ，pre_close 相对前收动了 ≥1 分 | `前收 / pre_close`（公布价优先；与 CQ 参考价差 >1 分时带 `cq_reference_disagrees`）。**厂商 bar** 上 pre_close 与参考价差 ≤1 分时改用 `前收 / CQ参考价`（带 `vendor_pre_close_rounding`） | `cq_pre_close` / `pre_close_over_cq` |
| 有 CQ，没有 pre_close | `前收 / CQ参考价` | `cq` |
| 有 CQ，pre_close 没动，但 **qfq 自己可分辨地动了**（`|q_step-1|` > 舍入界）且与 CQ 台阶同幅 | `前收 / CQ参考价`，带 `pre_close_missed_action` | `cq_qfq` |
| 有 CQ 的 `cq_qfq`/`cq` 台阶，前 5 根 bar 内已有一次同幅（≤0.2%）的**无 CQ** 台阶 | 1，带 `duplicate_of_earlier_step`（同一次除权不数两次） | `cq_already_counted` |
| 有 CQ，价格没动，qfq 也没动（小于 1 分的派息） | 1 | `cq_no_move` |
| 无 CQ，pre_close 动了且 step>1，qfq 可分辨地同向同幅 | `前收 / pre_close` | `pre_close_qfq` |
| 无 CQ，pre_close 动了但 step<1 | 1（公司行为只会让累计因子变大；这是坏收盘，如 600664.SH 08-31 的 9.00 vs 9.12） | `none` + `decreasing_step_rejected` |
| 无 CQ，也没有 longhu 数据（抓取失败） | `前收 / pre_close`，带 `pre_close_only` | `pre_close_only` |
| 无 CQ、无 pre_close 信号，qfq 台阶 > 1% + 3×舍入界 | qfq 台阶，带 `qfq_only` | `qfq_only` |
| 其它 | **恰好 1**（普通交易日永不漂移） | `none` |

舍入界：qfq 取整到 0.01，单日比值误差 ≤ `0.0051/qfq_prev + 0.0051/qfq_cur`。

**补抓 bar 的还原**（`restore_vendor_bars`，在一切台阶判断之前）：`selected_provider='longhuvip_composite'`
的 bar 带上抓取日（`available_at` 的北京日期）。对每个 `bar 日 < CQ 日 ≤ 抓取日` 的厂商除权记录，按
`raw = qfq×(1+送转) + 派现` 从新到旧逆推 close 与 pre_close，四舍五入到分；只有当厂商**今天**的 qfq
序列（撤销其后全部 CQ 后）隐含的原始收盘离还原值比离库存值更近时才采用，否则原样保留并记录。
还原只用于推导，**不回写 bar**。本窗口 18 只票 33 根 bar 被还原（全部是 09-07..09-09、抓取日 09-10），
全部通过校验。审查前的实现没有这一步：这些 bar 在 09-07 产生假的 `pre_close_qfq` 台阶，再在真实
除权日由 `cq_qfq` 记第二次，同一次派息被数两次（14 只票），当时只是被 09-07/09-09/09-10/09-17 的
tushare 校验点掩盖。

**`cq_qfq` 的确认**：审查前只检查"CQ 台阶超过舍入界"，平的 qfq 序列（`q_step = 1`）会"确认"任何
小于约 0.3%~0.47% 的派息；现在要求 qfq 台阶本身超出舍入界。

**canonical 缺 bar 的处理**（`fill_bar_gaps`）：2026-09-07 只有 5,035 根 bar（前后约 5,140），
次日 bar 的 pre_close 指向的是缺失那天的收盘。厂商当天有成交时，用 qfq 收盘乘下一根的帧比例、
再逆向撤销其间的每条 CQ 重建那天的原始收盘，作为**虚拟 bar** 参与台阶计算，**永不写入**。
厂商当天也没有 K 线（真停牌）则不插，复牌日的 pre_close 仍是有效证据。
没有任何 longhu 数据、且中间有交易所日历上的交易日时，pre_close 的变动无法区分行情与除权：
该步 `unresolved`，**链条在此截断**，之后的日期不写（留给下次有证据时补），不猜。

**锚点**：每只票窗口开始前最新一条可提升因子（tushare 或 `longhu_qfq_derived`），且当天必须有
canonical bar。完全没有锚点、且第一根 bar 在窗口内的是新股，从 1.0 起（tushare 口径），证据里
写明 `new_listing_starts_at_one`；有历史 bar 却从无因子的（指数：000001.SH、000300.SH、000688.SH、
399001.SZ、399006.SZ）记为 `no_factor_lineage`，不写。

**窗口内已存的 tushare 因子 = 校验点**：推导值与之比较（>0.2% 记为分歧并在报告里列出），
bar 上写**已存的 tushare 值**（按库里的 numeric 原值，不经 float），链条从它继续——真实数据永不被覆盖。
**唯一例外：价格否定的校验点**。从上一个被采用的已存值（或锚点）到本日，比较三个量：已存序列自己的
变动、推导台阶之积、交易所自己的台阶之积（`前收 / pre_close`）。已存值的变动与价格不符（>0.2%）而推导
与价格相符时，**不采用**该已存值：bar 写推导值，已存行原样保留不动，报告的 `stored_factor_conflicts`
列出该票，需要人工决定。生产里只有 300176.SZ：交易所 08-21 配股除权（pre_close 4.72 vs 前收 5.23），
tushare 一直到 09-07 都是 4.5917，09-09 才跳到 5.0878，而 09-08 4.59 → 09-09 4.58（pre_close 4.59）
没有任何价格断点；照写会在 09-09 制造 +10.6% 的假涨幅。缺 pre_close（或经过虚拟 bar）时没有裁决，
仍采用已存值。本车道自己写过的 `longhu_qfq_derived` 行永不受此裁决。同一天多条 tushare 路线
数值只差第 5~7 位时（600601.SH 08-31：`tushare_primary` 6471.278 vs `tushare_super_sdk` 6471.28），
优先 bar 上已经带着的那条，避免换值制造舍入抖动。

**来源命名**（来源名必须与真实来源一致）：推导行写 `quant.daily_adjustment_factors`，
`provider='longhu_qfq_derived'`，`raw` 带 `factor_semantics='corporate_action_cumulative'`、
`source='longhuvip:GetKLineDay_W14'`、`method='longhu_cq_preclose_qfq_v2'`、锚点、step、basis、
flags 与逐日证据（前收、pre_close、qfq、CQ、参考价）；`available_at` 是写入时刻，从不回填过去。
`tushare_normalization.promotable_adjustment_factor` 把 `longhu_qfq_derived` 作为**唯一**的非 tushare
可提升来源，而且要求**显式**声明累计语义（缺省即拒）；发布守护查询、工作清单、个股窗口就绪度
都改用同一个 SQL 孪生 `promotable_factor_evidence_sql()`。

### 0.4 写入（`longhu_adjustment_factors.persist_factor_date`，每个交易日一个事务）

同一事务内：upsert 推导证据行（值不变不改 `available_at`）→ 同值写 `canonical_bars_daily` 与
`market_bars_daily`（只改确实不同的行）→ 对本日没有计划行的票（无血缘/截断/longhu 抓取失败）清掉
**没有任何可提升证据行带着同一数值**的因子（按**值**判断：tushare 行旁边的占位 1 也会被清；NULL 是诚实的
"未知"）→ 给本日 `longhuvip_composite` 占位证据打 `superseded_at` 标注（不删），`superseded_by` 写
**实际提升到该票 bar 上的那一行的 provider**（tushare 校验点就写 tushare 路线；没有替换则写
`none: no factor promoted onto this bar`）→（repair）清掉该日的退休台账。

longhu 抓取失败的票（≤5% 时整轮不算失败）**不再**只凭 pre_close 推导：没有 CQ、没有 qfq，会漏掉所有
无价格信号的除权，而且写下的行会变成以后没人重看的校验点。这些票在本轮保持 NULL（`held=longhu_fetch_failed`），
下一轮的 hole 补洞带着证据重试。**绝不存在"先整体 NULL 化、稍后再回填"的中间态**，
所以任何时刻都不会让研究窗口成片失败关闭。该函数已登记进
`tests/test_adjustment_factor_semantics_guard.py` 的 `PINNED_BAR_FACTOR_WRITERS`。

### 0.5 实测（生产库只读，2026-09-19）

**验证模式**（`validate --from 2026-06-01 --to 2026-08-26`，用 longhu 证据重算，与库里的
tushare 因子逐步比较；5,555 只，longhu 抓取失败 0）：

| 指标 | 数值 |
|---|---|
| 相邻 bar 对 | 221,117 |
| tushare 真实除权（\|台阶-1\|>1e-5） | 863 |
| 命中（台阶误差 ≤0.2%） | **863（100%）** |
| 漏检 / 幅度错 | 0 / 0 |
| 误报 | 3（300176.SZ 配股：交易所 08-21 除权、pre_close 4.72 vs 前收 5.23，tushare 当天没变；603221.SH +0.11%；688757.SH +0.10%——后两者 longhu 与交易所都显示小额派息而 tushare 没记） |
| tushare 自身舍入抖动（±1e-6 来回） | 30 对，不计为除权 |
| 链式累计误差（窗口首日锚定，推到末日） | 中位 0，p99 3.6e-5，>0.2% 的只有 300176.SZ（10.8%，即上面的配股） |

按 basis：`cq_pre_close` 838/838、`pre_close_over_cq` 25/26（另 1 条是 300176 配股）；
`none` 220,241 步全部正确。

**修复 dry run**（`repair`，审查修正后重跑，2026-09-19 下午，只读；窗口从数据推导：08-27 有
5,551/5,552 根 NULL，09-01、09-04..09-18 带未标注占位证据 → `2026-08-27 .. 2026-09-18`，17 个交易日）：

- 5,566 只票；longhu 抓取失败 0；新股 11 只从 1.0 起；截断 0；无血缘 5 只（指数）。
- 补抓 bar 还原：18 只票 33 根（全部校验通过，0 根被拒）。
- 窗口内识别除权 248 次：`cq_pre_close` 244（其中 8 次厂商 pre_close 半分取整、改用 CQ 参考价）、
  `cq` 2、`pre_close_over_cq` 2；**`pre_close_qfq` 0、`cq_qfq` 0**（审查前的 18 + 14 次全部是补抓 bar
  造成的假台阶及其重复计数）。拒绝：`decreasing_step_rejected` 7、`pre_close_signature_rejected_by_qfq` 4。
- 与窗口内已存 tushare 因子比较 53,731 次，分歧 3 次，全是 300176.SZ（09-09、09-10、09-17，
  价格否定、未采用，见上）。审查前报告的"7 只票 09-07 tushare 晚一天"是**推导的错**，不是 tushare 的：
  tushare 在厂商 CQ 日（09-08/09-09/09-10）变值是对的；600517.SH 09-17 的 0.203% 是厂商 pre_close 取整，
  也已修正。
- **只用 longhu 的回放**（只保留 09-04 之前的 tushare 校验点、推到 09-17，再与库里 09-17 的 tushare
  因子比）：5,145 只里偏差 >0.1% 的只剩 300176.SZ（-9.75%，即 tushare 事后补记的配股）；审查前是 15 只。
- 每日计划：08-27 回填 5,546 个 NULL（全部来自已存 tushare 行）；09-01、09-02/03、09-07、09-09、
  09-10、09-17 以已存 tushare 截面为主（300176.SZ 在 09-09/09-10/09-17 写推导值）；09-04、09-08、09-11、
  09-14~09-16、09-18 每天约 5,000~5,100 行 `longhu_qfq_derived`。`market_bars_daily` 同步约 5,000~5,500 行/日。
- 守护查询：现在 `canonical_bars_daily=35,573`、`market_bars_daily=0`，**应用后预计 0 / 0**。
- **值校验**（新）：bar 的因子不等于当日任何可提升证据行的，窗口内现在 61,087 根（其中 09-01/07/09/10/17
  约 25k 根是 tushare 行旁边的占位 1，守护查询看不见），**应用后预计 0**；窗口外（2026-01-01..08-26）为 0。
- 窗口内 NULL：现在 5,645 根，应用后 72 根，全部是上面 5 只指数（`no_factor_lineage`）。

### 0.6 运维

```
# 只读：方法验收（可随时重跑）
python scripts/adjustment-factor-maintenance.py validate --env-file G:\StockPlatform\config\runtime.env

# 只读：一次性修复的计划（窗口自动推导；--from/--to 可覆盖）
python scripts/adjustment-factor-maintenance.py repair --env-file G:\StockPlatform\config\runtime.env

# 写入：每个交易日一个事务，结束后守护查询 + 值校验 + 逐日回读；任一非 0 则退出码 1
python scripts/adjustment-factor-maintenance.py repair --apply --env-file G:\StockPlatform\config\runtime.env

# 应用后验收（只读）：status 的 identity_factor_leaks 0/0 且 factor_value_mismatches 0/0，
# 再跑一次 repair dry run：plan 里每天 replaces_other_value=0、fills_null=0（只剩 5 只指数的 NULL）
python scripts/adjustment-factor-maintenance.py status --lookback-days 30 --env-file G:\StockPlatform\config\runtime.env

# 夜间车道（盘后阶段与 04:30 任务走同一个入口）
python scripts/adjustment-factor-maintenance.py sync --lookback-days 30 --env-file G:\StockPlatform\config\runtime.env
```

- `repair` 不带 `--apply` 时根本不 import `app.main`，用 `default_transaction_read_only=on` 的独立连接，
  服务端拒绝任何写入——dry run 对生产安全是**构造保证**，不是靠小心。
- `repair` 幂等：锚点取窗口开始前的因子，窗口内自己写过的推导行会被重算（同值不改）。
- 夜间 `sync`：工作清单与退休台账语义不变（覆盖率 <95% 的日期；覆盖率被拒=skipped；
  longhu 失败或推导覆盖不足=failed，退出码 1）。另外，回看窗口里"覆盖率已够、但仍有带血缘票为 NULL"
  或"bar 的值不等于任何证据行"（值校验）的日期作为 `hole_dates`，只补这些 bar、不进台账——抓取失败、
  截断留下的洞，以及一次性修复之前夜间车道先跑时 tushare 日上残留的占位 1，下次自动补上。
- 守护查询只问"有没有可提升证据"，看不见 tushare 行旁边的错值；`status` 与 `repair --apply` 的回读
  都带窗口内的值校验 `factor_value_mismatches`，`repair --apply` 在它非 0 时同样退出码 1。
- 全市场一次抓取约 5,500 次调用（16 并发，实测 95~105 秒），无人为限速；单票失败按轮重试 3 次；
  超过 5% 的票失败则整轮算 provider 失败、不写。
- 盘后阶段顺序、非门控、回执语义都不变（第 5 节）。

### 0.7 仍然触及因子/日控制面的 tushare 依赖（本次有意未删）

1. `app/daily_control_plane.sync_full_market_daily_controls`：longhu 截面不满足短路条件（或 longhu
   未配置）时回落到 `full_market_daily_controls_sync.sync` 的四 API tushare 拉取
   （`adj_factor`/`daily_basic`/`stk_limit`/`suspend_d`）。owner 上 longhu 日会短路，不会走到。
2. `app/full_market_daily_controls_sync.py` 本身（上面的回落路径，含 `market_bars_daily` 镜像）。
3. `app/tushare_normalization.normalize_rows` 的 `adj_factor` 分支：任何仍到达的 tushare 因子行照旧入证据并提升。
4. `app/annual_daily_backfill.py` 的历史年度回补（tushare 截面的集合式提升）。
5. `app/intraday_watchlist_service.py`：自选股入池时 `fetch_supplemental("watchlist_adj_factor", adj_factor)`。
6. `app/capability_registry.py` 的 `adj_factor` 条目（`super_get/super/primary`）——状态报告已不再引用它。
7. `app/stock_study_readiness_repository.raw_api_window_summary` 读 `quant.tushare_raw_records`
   （P1 项的原始记录摘要；`adj_factor` 条目本身按因子表判定）。
8. `app/core_daily_control_sync.py`（`main.sync_tushare_core_endpoint`，ingestion 路由 `sync_tushare_core`）：
   对显式股票池逐只拉 tushare `adj_factor`/`daily_basic`/`stk_limit`/`suspend_d`，经 `fetch_catalog` →
   `normalize_rows` 入证据并提升；tushare 可达时会改写 bar 上的因子。owner 没有 tushare key，手动调用才会走到。
9. 04:30 计划任务的**注册描述**（`scripts/windows/install-adjustment-factor-task.ps1`）已改为 longhu；
   已安装的任务要重新运行安装脚本才会刷新描述文字（动作与参数不变，不重装也照常工作）。

### 0.8 已知风险与分歧

- **300176.SZ 需要人工决定**：tushare 漏记 08-21 配股、09-09 才补记。修复写推导值（沿用 4.5917，
  窗口内无假跳变），08-21 的真实除权在 tushare 时代的 bar 上仍未体现（08-20→08-21 复权序列有约 -10%
  的假跌，修复前就存在）。可选：人工把 08-21..最新的因子按 08-21 的 pre_close 证据重锚到 5.0878（需改写
  tushare 时代的 bar，本次不做）。
- **09-07..09-09 的 bar 价格本身仍是前复权**：还原只用于推导，不回写 bar。18 只票 33 根 bar 的 close
  比原始价低一次派息（0.05%~0.5%），`close×adj_factor` 在这几根上有同样大小的误差。修正需要改写
  bar 价格，不在因子车道范围内。
- **配股字段未验证**：`f1/f2` 语义只有 1 个样本；有 pre_close 时以公布价为准。
- **补抓的 longhu bar**：推导已按抓取日还原（见 0.3），但依赖 `available_at` 如实记录抓取时刻；
  若某次补抓只改价格不改 `available_at`，还原不会触发（厂商序列校验只能拒绝错误还原，不能发现漏掉的）。
  建议后续把 `parse_daily_kline_payload` 的 "unadjusted" 注释改正，并让补抓历史日时就用 CQ 反推原始价入库。
- **longhu 单源**：CQ 缺失且 pre_close 也不动的除权（验证期 0 例）只能靠 qfq-only 阈值（>1%）兜底。
- **没有 tushare 之后的校验**：从今往后没有新的独立真值；`validate` 只能对历史 tushare 期重跑。

---

## 1. 语义：`adj_factor` 是累计复权因子，不是"当日不变"的标记

`quant.canonical_bars_daily.adj_factor` 与 `quant.market_bars_daily.adj_factor`
都被下游当作 **累计（后复权 / hfq 风格）公司行为因子** 使用：`close * adj_factor`
必须在不同交易日之间可比。整个服务里所有跨日价格比值（`research_prices.adjusted_bars`、
`factor_lab`、`feature_snapshot_repository`、`post_close_structures`、
`ten_day_leader_ranking`、`factor_sql_lab` 等）都建立在这一条上。

**规则（已写入 `AGENTS.md`，并由 `quant-service/tests/test_adjustment_factor_semantics_guard.py` 守护）：**

> 不供应公司行为历史的数据源，`adj_factor` 一行都不写，绝不写占位的 `1`。

原因是 `1.0` 既不是 `NULL` 也不是 `<= 0`，因此它能通过 **每一个** 本应失败关闭的
复权检查，让消费者在不知情的情况下拿到 **未复权** 的序列。`NULL` 才是"尚未取到因子"
的诚实取值 —— `research_prices.adjusted_bars` 就是为了识别 `NULL` 而写的。

对应关系：

| 取值 | 含义 | 下游行为 |
|---|---|---|
| 真实累计因子（> 0） | 已存的 tushare 累计因子，或 `longhu_qfq_derived` 推导值（第 0 节） | 正常复权 |
| `NULL` | 尚未取到（待补） | 窗口**末尾**的短缺口且无除权迹象时按第 1.1 节顺延（`adj_factor_carried_forward`）；否则失败关闭：`adj_factor_missing` / `data_quality_blocked` / `adjustment_pending` |
| `1`（伪造占位） | **禁止** | 静默产生未复权序列 |

> `1` 本身并不一定是错的：新上市个股确有合法的 `adj_factor = 1`。判定范围的谓词
> 永远是"该 symbol+date 存在 `same_day_identity_only` 占位证据且没有任何
> tushare 行"，**不是** `adj_factor = 1`。2026-09-04 之前有 96,134 行合法的 1.0。

---

## 1.1 末尾缺口顺延（carry-forward）：`NULL` 不等于"整只股票黑屏"

因子抓取是**独立于行情的另一条车道**（第 5 节），所以 longhu 收盘后最新的一两个交易日
必然先落地为 `adj_factor IS NULL`。如果每个跨日消费者都对这种"车道晚了一步"失败关闭，
那么被扣分、被剔除的是**全市场每一只股票**，与个股自身毫无关系 —— 一只更好的股票会
因为平台的数据时序而被挤出推荐池。这条规则就是为了消除那种误伤。

**规则（唯一实现：`quant-service/app/research_prices.py` 的 `resolve_factors()`，
`adjusted_bars()` / `ten_day_leader_ranking` / `effectiveness.simulate` 都调它）**

某个窗口里的 `NULL` 因子，**同时**满足以下四条时，按"窗口内最后一个真实因子"顺延：

1. **只在末尾**：`NULL` 之后不再有真实因子。窗口中间的 `NULL`（后面还有真因子）
   是历史缺口，不是车道滞后，**继续失败关闭** `adj_factor_missing`；
2. **有锚**：窗口内至少存在一个真实因子（`> 0`）。整窗全 `NULL` 无从顺延；
3. **有界**：顺延不超过 `MAX_CARRIED_FACTOR_SESSIONS = 5` 个交易日。超过就不再是
   "抓取晚了一两天"，而是"没人在抓"，必须失败关闭；
4. **无除权迹象**：缺口内每一天的 `pre_close` 与前一交易日 `close` 之差
   ≤ `CORPORATE_ACTION_PRICE_TOLERANCE = 0.01`（A 股报价到分）。任何一天对不上，
   就是交易所在告诉我们当天除权除息，**改判** `corporate_action_unresolved`。
   窗口里**根本没有 `pre_close`** 时什么也证明不了，同样失败关闭 `adj_factor_missing`。

满足时返回标记 `adj_factor_carried_forward`，顺延天数经
`research_adj_factor_carried`（逐行）/ `carried_forward_sessions()` 向上暴露。

**窗口必须按交易日升序**：第 1 条"只在末尾"、第 4 条"与前一交易日 `close` 比"
说的都是**相邻交易日**，所以时间顺序是这条规则的前置条件，不是调用习惯。
过去它是一条没写下来的默认，倒序（最新在前）的窗口会把每一句都悄悄读反：
车道滞后看起来像**开头**的洞而被拒，连续性检查也在和错的邻居比。
现在 `resolve_factors()` 自己按 `trading_date` 排序（`ascending_order()`），
并把 `factors` 与新增的 `carried_positions` **按调用方给的行序**返回——
倒序窗口里被顺延的是**前**几行，调用方不能再假定它们是末尾几行。
窗口里的行没有可比较的 `trading_date` 时保持调用方顺序（无从排起，凭空造序更糟）。

**这条护栏到底建立在什么之上**（写出来，读的人才好给残余风险定价）：

- **唯一证据就是 `pre_close` 连续性**，没有别的。这条路径上没有分红送转数据源，
  公司行为只因为"后一天的 `pre_close` 与前一天的 `close` 对不上"而被发现。
- **longhu 晚上的 `pre_close` 是厂商推导的**，不是交易所字段：
  `longhuvip_composite` 的 bar 带的是厂商自己的前收。用 2026-09-10 ~ 09-17
  的公司行为实测，特征出现在 **76 例中的 74 例**——探测器够用，但不是保证。
- **漏检的公司行为在幅度上有界**（不是"不存在"）：它必须让 `pre_close` 偏离
  小于 `CORPORATE_ACTION_PRICE_TOLERANCE = 0.01`，即相对幅度小于
  `CORPORATE_ACTION_PRICE_TOLERANCE / close`。100 元的票是 1 个基点，
  **2 元以下的票约 1%**——残余误差就住在这里。
- **五个交易日的上界数的是窗口里的 bar 行**，不是日历日、也不是交易所日历上的
  session：窗口稀疏（个股停牌、或查询本身漏了行）时顺延的是五**行**，
  可能横跨更多实际交易日。需要日历含义的调用方必须传稠密窗口。
- **盘后阶段顺序只保证"先尝试"**：`POST_CLOSE_STAGE_ORDER` 把
  `adjustment_factors` 排在所有读因子的阶段之前，并由
  `tests/test_post_close_refresh.py::test_the_factor_fetch_precedes_every_stage_that_reads_a_factor`
  钉住，但它是**非门控**阶段——抓取失败或被覆盖率拒绝时，末尾的 `NULL`
  照样留给这条规则去回答。顺序不等于因子已经到手。

**为什么这不是当年那个 `1.0` 占位：**

- `1.0` 占位在**绝对值上就是错的**：它宣称累计因子等于 1，`close * adj_factor`
  变成一条未复权序列，与它前面那段已复权的历史**不可比**，而且悄无声息。
  顺延保留的是**窗口内其余各日同一个基准**，所以该窗口里每一个跨日比值
  与最后一个已抓取交易日当时同样有效。
- 公司行为很稀有（全市场约 5,500 只，每天个位数），而且**不是不可见的**：
  交易所会按复权后的价位重新发布当日 `pre_close`，除权除息日因此自己
  "举手"（`pre_close ≠ 前收`）。所以对一段没有举手的短末尾缺口，
  "因子不变"是**正确的中性假设**，而依据是行情本身，不是猜测。
- **绝不回写**：顺延只活在一次请求的研究视图里。
  `quant.canonical_bars_daily.adj_factor` 保持 `NULL` 直到因子车道推导出真因子，
  `tests/test_adjustment_factor_semantics_guard.py` 守护的正是这一条。

**顺延不扣分**：`app/recommendation_generation.py` 的 `UNPENALIZED_FLAGS` 把
`adj_factor_carried_forward` 排除在扣分计数之外，也不在 `hard_flags` 里 ——
它描述的是平台抓取状态，不是个股缺陷；但它照样写进 `risk_flags`，读的人能看见基准。
`adj_factor_missing`（完全没有可用基准）与 `corporate_action_unresolved`
（有除权迹象且无法建模）仍然是硬标记，仍然扣分。

**顺延的上界只是"先尝试"**：`POST_CLOSE_STAGE_ORDER` 里 `adjustment_factors`
排在 `close_strategy_decision` / `post_close_strategy` / `watchlist_main_wave` /
`research_snapshot` **之前**，当晚才有机会先把真因子抓回来；
由 `tests/test_post_close_refresh.py::test_the_factor_fetch_precedes_every_stage_that_reads_a_factor`
钉住。但该阶段是非门控的（第 5.1 节），顺序保证的是**尝试次序**，不是因子已到手。

**覆盖范围（有意为之的边界）**：`app/factor_lab.py` 的 `adjusted_price(row)` 是
逐行接口，一行数据没有相邻交易日的收盘可比，按 `adjusted_value()` 保持严格；
`app/factor_sql_lab.py` 在 SQL 里用 `WHERE bar.adj_factor>0` 过滤，属于集合式实现，
本轮未纳入。

`app/watchlist_main_wave.py` 的 `normalize_bars()` **已改为走同一条规则**
（原先逐行丢弃 `NULL` 因子的 bar）：按 symbol 取升序窗口调 `resolve_factors()`，
可解析（完整，或"有界末尾缺口且无除权迹象"）就保留**全部** bar，被顺延的行带
`adj_factor_carried`，并经 `build_examples()` 把 `adj_factor_carried_forward`
写进当前行 payload（`metrics.current_scores[*].quality_flags`），
整轮再报一个 `metrics.carried_forward_factor_symbols`。
被规则拒绝的窗口（中间有洞 / 无锚 / 超界 / 有除权迹象）**整只剔除**——
逐行丢弃会把幸存的行拼成一条"看起来连续"的序列，60 日特征于是悄悄跨过缺失历史，
那才是真正危险的那一半。因为要读 `pre_close`，
`run_watchlist_main_wave_research()` 的 bar 查询补上了 `b.pre_close` 列。

---

## 2. 本次代码改动（已包含在本分支）

1. `app/longhu_market_sync.py` `build_control_rows()`：不再生成 `adj_factor` 行，
   返回值只有 `{"stk_limit": ...}`。
2. `app/longhu_market_repository.py`：不再 `persist_rows(..., "adj_factor", ...)`；
   `fetch_runs` 回执里的 `control_semantics.adj_factor` 改为
   `not_supplied_by_vendor; fetched separately from tushare adj_factor`。
3. `app/tushare_normalization.py`：因子行写入 `daily_adjustment_factors` 仍保留为证据，
   但只有 `promotable_adjustment_factor(row, provider_key=...)` 为真才会
   `UPDATE canonical_bars_daily`。**两个条件必须同时成立**：provider 以
   `tushare` 开头（`PROMOTABLE_FACTOR_PROVIDER_PREFIX`），**且**
   `factor_semantics` 缺省/为空或等于 `corporate_action_cumulative`。
   只看语义是不够的：一个**干脆不写 `factor_semantics`** 的厂商占位
   （`{'ts_code':…, 'adj_factor':'1'}`）会原样复现最初的缺陷，只是少了一个键；
   provider 这一半让新来源默认被拒，必须显式加入才可能被提升。
   集合式写入用同一条规则的 SQL 孪生 `promotable_factor_predicate_sql()`。
4. `app/daily_control_plane.py` `_longhu_control_status()`：短路门槛按控制项拆分，
   去掉 `factor_rows` 项，回执带 `satisfied_by_vendor` / `pending_controls` /
   `adjustment_state`。这个 dict 就是 `sync_full_market_daily_controls()` 的返回值，
   会原样落进盘后 `core_daily_controls` 阶段回执，所以它**也必须和退休台账一致**：
   在同一个事务里读一次台账（`adjustment_retirement_details`），该日期已退休时
   `adjustment_state='retired'`、`pending_controls=[]`、`retired_controls=['adj_factor']`、
   另带 `adjustment_retirement`（含要清的 `run_key`），`quality_note` 写明"不会再自动补"；
   未退休时 `pending_controls=['adj_factor']`、`retired_controls=[]`、
   `adjustment_retirement=None`，与改动前一致。
5. `app/full_market_daily_controls_sync.py`：`sync()` 新增 `apis` 参数；两条
   `is_suspended=false` 复位、涨跌停回填、停牌回填、复权回填分别按各自 API 是否在
   `apis` 中做闸门。**只跑 adj_factor 的任务绝不会清掉当日停牌标记。**
6. `app/adjustment_factor_maintenance.py`（新）：`pending_dates()` / `sync()` /
   `post_close_sync()`，对覆盖率不足的交易日以 `apis=('adj_factor',)` 逐日补取；
   覆盖率被拒的日期记 `skipped` 并累计"连续被拒**天数**"（按晚上计，不是按调用次数
   计——post-close 流水线一晚上要跑十几次），见第 5 节。
7. `app/capability_registry.py`：`adj_factor` 独立分支，
   `preferred_providers=('super_get','super','primary')`、`status='verified'`
   （`tushare_primary` 的 adj_factor 当前是 `failed`/ConnectError）。
8. `app/daily_control_plane.py` `status_payload()`：`limits_ready` 与四态
   `adjustment_state ∈ {complete, pending, retired, absent}` 拆开；
   **`ready` 不再包含复权项**；新增 `adjustment_state`、`adjustment_pending_rows`、
   `adjustment_retirement`、`research_adjustment_ready`。
   `retired` 与第 12 项的个股窗口就绪度**用的是同一个词、同一份证据**：该交易日已被
   退休台账剔出工作清单，说 `pending`（"已排队，会来"）就是假承诺。证据由
   `adjustment_retirement_details(connection, rows)` 单独查一次台账拿到，再以
   `status_payload(rows, retired_dates=...)` 传进来——`status_payload` 仍然是纯函数，
   只吃行、不吃连接。`reason` 里直接写出要清的 `run_key`；退休**同样不阻断**门槛
   （`state` 与 `ready` 不受影响），只是把"等"换成"该做什么"。
   调用方：`app/main.py:full_market_daily_control_status()`（与行同一个事务里查台账）、
   `scripts/equity-readiness.py`、`scripts/verify-equity-control-recovery.py`。
   这两件事各有测试兜底：`FullMarketDailyControlStatusWiringTests` 直接驱动
   `app.main.full_market_daily_control_status()`（替换 `main.db`），断言台账查询与行查询
   **在同一个事务的同一条连接上**、问的正是这些行里的日期、并且答案真的进了 `status_payload`；
   `StatusPayloadCallSiteGuardTests` 用 AST 走 `quant-service/app` 与 `scripts` 下所有
   `.py`，要求每一个 `status_payload(...)` 调用都显式传 `retired_dates=`——
   固定装置里没有台账的用例写 `retired_dates={}`，即"这里确实没有台账"，
   而不是忘了传。漏传的新调用方会当场失败，而不是悄悄又说回 `pending`。
9. `app/ten_day_leader_rotation_repository.py`：两处覆盖度判定去掉
   `adj_factor IS NOT NULL`（改由 `quality_status` 回答"是否有结算 bar"）；
   `latest_full_market_date` 的 CTE 列改名 `adjusted_symbols` → `settled_symbols`
   （它数的就是结算 bar）；`TenDayRankingInputs.adjusted_symbols` 改名
   `adjustment_covered_symbols`，并由 `ten_day_leader_rotation_service` 写进
   `source_status`，所以轮动车道的复权覆盖率在落库的 run 里可见，而不是只存不读。
10. `app/effectiveness/execution.py` `simulate()`：`adj_factor` 移出必填字段，
    无法解析的 `NULL` 返回 `adjustment_pending`，因子不一致（或缺口内出现除权迹象）
    才是 `corporate_action_unmodeled`。判定统一交给第 16 项的 `resolve_factors()`。
11. `app/annual_daily_backfill.py`：
    - `reconcile_suspensions()`：候选行要求 `provider LIKE 'tushare%'` 且排除
      `raw->>'factor_semantics' = 'same_day_identity_only'`。**没有这条，下面的修复不持久。**
    - `_persist_adj_factor()`：这是**第二处**"因子行 → bar 字段"的提升，原本对两张 bar 表
      完全没有任何 provider / 语义 / 排除条件，是被修掉的那个缺陷的同一形状。现在
      provider 不是 tushare 就直接返回，bar `UPDATE` 再带上 `{_PROMOTABLE_FACTOR_SQL}`。
12. `app/stock_study_readiness_repository.py`：`adj_factor` 条目改为**按 symbol 判定**。
    计数只算**真因子**（`provider LIKE 'tushare%'` 且语义可提升），因为第 4 节步骤 4
    只给占位行打标注、从不删除，`count(*) > 0` 会在恰恰还没修好的 symbol 上永远报
    `ready`。四态：该 symbol 窗口内所有结算日都有真因子 → `ready`；缺的日期里**有任何
    一天已被工作清单退休**（见 5.3 的"连续被拒 5 次"）→ `retired`，note 里带该日期的
    `run_key` 与被拒原因——它不会被补，再说"排队中"就是假承诺；缺的日期**全部**在
    维护任务的待办清单上 → `pending`；否则（含混合情况）→ `missing`，失败关闭。
    条目另带 `settled_sessions` / `pending_dates` / `retired_dates` / `missing_dates`。
    判定用 `adjustment_factor_maintenance.pending_and_retired_dates_between()`；
    `pending_dates_between()` 本身**默认就查退休账本并剔除退休日期**——"pending"
    是一句"有人会来修"的承诺，只有 `sync()`（它自己分别上报两份清单）用
    `include_retired=True` 取原始覆盖率清单。
13. `scripts/adjustment-factor-maintenance.py`：
    `sync --lookback-days N --env-file ... --dry-run`，ASCII-only stdout；
    依赖组装挪到 `app.main.adjustment_factor_maintenance_dependencies()`，
    所以 CLI、盘后阶段、计划任务三个入口不会各自漂移。
14. `app/post_close_refresh.py` / `post_close_refresh_service.py` / `app/main.py`：
    新增**非门控**盘后阶段 `adjustment_factors`，见第 5 节。
15. `app/full_market_daily_controls_sync.py`：每个 `blocked` 回执带 `blocked_by`
    ∈ {`coverage`, `provider`, `executor_saturated`}，覆盖率不足单独抛
    `ControlCoverageError`。调用方据此区分"这一天的日线截面不够"与"provider 挂了"。
16. **末尾缺口顺延（第 1.1 节）**：`app/research_prices.py` 新增
    `resolve_factors()` / `FactorResolution` / `carried_forward_sessions()`，
    `adjusted_bars()` 改为走它；`ten_day_leader_ranking` 与
    `effectiveness/execution.simulate()` 也改调同一条规则（不再各自判 `adj_factor`）。
    `recommendation_generation.UNPENALIZED_FLAGS` 让顺延不扣分；
    `feature_snapshot_repository` 报 `research_price_status='carried_forward'`；
    `intraday_factor_contracts` 的 `daily_rebound_state` 声明该标记。
    规则要读 `pre_close`，所以 `feature_snapshot_repository`、
    `watchlist_daily_factors`（两条查询）、`post_close_strategy_service`、
    `event_research`、`effectiveness/service.py` 的 bar 查询都补上了 `pre_close` 列
    —— 少这一列的窗口证明不了"没有除权"，只会一直失败关闭。

---

## 3. 执行顺序（release 2 历史；已被第 0.6 节 `repair` 取代，不要再执行）

> **顺序就是全部风险。** 第 4 节的 NULL 化一旦先于第 8/9 项代码上线，
> 每个 longhu 交易日的 `status_payload` 立刻变成 `state='blocked'`，
> 十日龙头轮动车道的覆盖日期 CTE 会返回空集，整条车道静默停产。

1. **发布**：把本分支合入并发布，让第 8 项（readiness 契约）与第 9 项
   （十日龙头谓词）**同一次发布**生效。发布后先跑一次只读校验：
   `python scripts/equity-readiness.py --date 2026-09-18`
   应当仍是 `state='ready'`（此时复权行还是占位的 1，`adjustment_state='complete'`）。
1b. **装计划任务**：`pwsh scripts\windows\install-adjustment-factor-task.ps1`
   （每天 04:30，见 5.2）。盘后非门控阶段随发布自动生效，不需要另外安装。
   两者都装好之后，新交易日不再需要人工补取；下面的步骤 2-6 只是**一次性修复**。
2. **补取缺失日期**：在 04:00-08:00 维护窗里跑
   `python scripts/adjustment-factor-maintenance.py sync --lookback-days 30 --env-file G:\StockPlatform\config\runtime.env`
   把 09-01 ~ 09-18 里还没有 tushare 因子的日期补齐。
   > `pending_dates()` 是向 `quant.daily_adjustment_factors` 提问"这张结算 bar 有没有
   > **真实**累计因子"，而不是看 bar 上的 `adj_factor` 是否非空，因此 **在 NULL 化之前
   > 就能正确列出受污染日期** —— 占位的 1 不会让这些日期看起来已完成。
3. **Dry run**：`python scripts/adjustment-factor-maintenance.py sync --dry-run --env-file ...`
   打印待处理日期，确认范围与预期一致（不发任何 provider 请求、不写库）。
   补取之后再跑一次 dry run，剩下的就是 tushare 也没有因子的日期。
4. **NULL 化**：执行第 4 节步骤 1。预计 35,573 行变为 `NULL`。
5. **回填**：执行第 4 节步骤 2 与步骤 3（canonical 与 market 两张 bar 表）。
   每补取一批新因子后都可以原样重跑，语句是幂等的。
6. **校验**：执行第 4 节步骤 5，并重新跑
   `python scripts/equity-readiness.py --date 2026-09-18`，
   期望 `state='ready'` 且 `adjustment_state='pending'`（**不是** `blocked`）。
7. **守护查询**：执行第 4 节步骤 6，必须返回 `0`，并纳入每次发布前的检查。
   同一条 SQL 由 `app/adjustment_factor_maintenance.identity_factor_leak_sql()` 提供，
   `tests/test_adjustment_factor_semantics_guard.py` 在有 `PGHOST` 时对它做实库验证。

可选：第 4 节步骤 4（给占位证据打 `superseded_at` 标注）。**优先标注而不是删除** ——
这些行是"厂商没有提供什么"的真实记录，删掉就毁掉审计链。

---

## 4. 修复 SQL（release 2 历史；基于 tushare 原始记录，已被第 0 节取代，不要再执行）

### 步骤 -1 —— 推导修复窗口（只读，必须先跑）

窗口不是写死的日期，而是**从数据里推导**出来的：受污染的范围就是"还没有被标注
`superseded_at` 的 longhu 占位证据"所覆盖的交易日区间。写死日期会和步骤 6 的守护查询
（不带窗口、且要求恒等于 0）失配——只要占位证据落在窗口之外，步骤 1 就补不到它，而守护
查询照样会看见它。

```sql
SELECT min(trading_date) AS from_date, max(trading_date) AS to_date
  FROM quant.daily_adjustment_factors
 WHERE provider = 'longhuvip_composite'
   AND raw->>'factor_semantics' = 'same_day_identity_only'
   AND raw->>'superseded_at' IS NULL
\gset
```

`\gset` 把结果写进 psql 变量 `:from_date` / `:to_date`，下面每一条语句都用它们，不再出现
任何字面日期。用其他客户端跑时把这两个值原样绑定进去即可
（`scratchpad\release2-operator.ps1` 的内嵌 Python 程序就是这么做的：
`CAST(%(from_date)s AS date)`）。

**一致性前置断言**：步骤 0 在推导出的窗口内的 `would_null` 合计，必须等于步骤 6 那条
不带窗口的守护查询在 `canonical_bars_daily` 上的返回值。两者不等，说明窗口没有覆盖全部
污染行——此时**不要**执行步骤 1，先查清差额来自哪些交易日。

> 2026-09 的实测值（只读实测 2026-09-19，生产库）：推导结果是
> **2026-09-01 ~ 2026-09-18**，未标注的占位证据 61,600 行；步骤 0 合计
> `identity_rows=61,558`、`would_refill=25,985`、`would_null=35,573`；步骤 6 守护查询
> `canonical_bars_daily=35,573`、`market_bars_daily=0`——`would_null` 与守护值一致，前置
> 断言通过。
>
> 顺带记下当时为什么范围要从 **2026-09-01** 开始而不是 09-04：09-01 同样是 longhu 占位日
> （5,235 行受污染的 canonical 行 / 5,266 行 NULL 的 market 行），漏掉它会在每个 60 日窗口
> 中间留下一天未复权空洞，而且它是最便宜的一天：`tushare_primary` 已经有当天 5,567 只的
> 完整截面。推导查询自己就会把 09-01 包进来——这正是推导优于手写日期的理由。

### 步骤 0 —— DRY RUN（只读）

```sql
-- 判定谓词是"该 symbol+date 存在占位证据"，不是 adj_factor = 1。
SELECT b.trading_date,
       count(*)::bigint AS identity_rows,
       count(*) FILTER (WHERE t.adj_factor IS NOT NULL)::bigint AS would_refill,
       count(*) FILTER (WHERE t.adj_factor IS NOT NULL AND t.adj_factor <> 1)::bigint AS would_change_value,
       count(*) FILTER (WHERE t.adj_factor IS NULL)::bigint AS would_null
  FROM quant.canonical_bars_daily b
  JOIN quant.daily_adjustment_factors ident
    ON ident.symbol = b.symbol AND ident.trading_date = b.trading_date
   AND ident.provider = 'longhuvip_composite'
   AND ident.raw->>'factor_semantics' = 'same_day_identity_only'
  LEFT JOIN LATERAL (
        SELECT f.adj_factor
          FROM quant.daily_adjustment_factors f
         WHERE f.symbol = b.symbol AND f.trading_date = b.trading_date
           AND f.provider LIKE 'tushare%'
         ORDER BY CASE f.provider WHEN 'tushare_super_sdk' THEN 0 WHEN 'tushare_super_get' THEN 1
                                  WHEN 'tushare_primary' THEN 2 ELSE 9 END,
                  f.available_at DESC
         LIMIT 1) t ON TRUE
 WHERE b.trading_date BETWEEN CAST(:'from_date' AS date) AND CAST(:'to_date' AS date)
   AND b.adj_factor = 1
 GROUP BY 1 ORDER BY 1;
```

### 步骤 1 —— NULL 化（只有占位证据的行）

`NULL` = "没有复权信息"，正是 `app/research_prices.py` 的 `resolve_factors()` /
`adjusted_bars()` 设计要识别的状态（末尾短缺口按第 1.1 节顺延，其余失败关闭）。

```sql
BEGIN;
UPDATE quant.canonical_bars_daily b
   SET adj_factor = NULL, canonicalized_at = now()
 WHERE b.trading_date BETWEEN CAST(:'from_date' AS date) AND CAST(:'to_date' AS date)
   AND b.adj_factor = 1
   AND EXISTS (SELECT 1 FROM quant.daily_adjustment_factors ident
                WHERE ident.symbol = b.symbol AND ident.trading_date = b.trading_date
                  AND ident.provider = 'longhuvip_composite'
                  AND ident.raw->>'factor_semantics' = 'same_day_identity_only')
   AND NOT EXISTS (SELECT 1 FROM quant.daily_adjustment_factors f
                WHERE f.symbol = b.symbol AND f.trading_date = b.trading_date
                  AND f.provider LIKE 'tushare%');
-- 预期：UPDATE 35573（先跑步骤 0 核对，再 COMMIT）
COMMIT;
```

### 步骤 2 —— 从库里已有的 tushare 因子回填 canonical

已有约 25,985 行可直接回填（09-01、09-07、09-09、09-10、09-17 的完整截面，
外加其余日期的逐票探针）。语句幂等，每补取一批新因子后原样重跑即可。

```sql
BEGIN;
UPDATE quant.canonical_bars_daily b
   SET adj_factor = t.adj_factor, canonicalized_at = now()
  FROM (SELECT DISTINCT ON (symbol, trading_date) symbol, trading_date, adj_factor
          FROM quant.daily_adjustment_factors
         WHERE trading_date BETWEEN CAST(:'from_date' AS date) AND CAST(:'to_date' AS date)
           AND provider LIKE 'tushare%'
           AND coalesce(raw->>'factor_semantics','') <> 'same_day_identity_only'
         ORDER BY symbol, trading_date,
                  CASE provider WHEN 'tushare_super_sdk' THEN 0 WHEN 'tushare_super_get' THEN 1
                                WHEN 'tushare_primary' THEN 2 ELSE 9 END,
                  available_at DESC) t
 WHERE b.symbol = t.symbol
   AND b.trading_date = t.trading_date
   AND b.trading_date BETWEEN CAST(:'from_date' AS date) AND CAST(:'to_date' AS date)
   AND b.adj_factor IS DISTINCT FROM t.adj_factor;
COMMIT;
```

### 步骤 3 —— 同样回填 quant.market_bars_daily

该表不需要 NULL 化：09-01（5,266 行）与 09-04 ~ 09-18（56,461 行）本来就 100% 为 `NULL`
—— longhu 路径从不写这张表（`app/full_market_daily_controls_sync.py` 是日常路径里唯一的
写入者，而 longhu 日会跳过它）。

```sql
BEGIN;
UPDATE quant.market_bars_daily b
   SET adj_factor = t.adj_factor
  FROM (SELECT DISTINCT ON (symbol, trading_date) symbol, trading_date, adj_factor
          FROM quant.daily_adjustment_factors
         WHERE trading_date BETWEEN CAST(:'from_date' AS date) AND CAST(:'to_date' AS date)
           AND provider LIKE 'tushare%'
           AND coalesce(raw->>'factor_semantics','') <> 'same_day_identity_only'
         ORDER BY symbol, trading_date,
                  CASE provider WHEN 'tushare_super_sdk' THEN 0 WHEN 'tushare_super_get' THEN 1
                                WHEN 'tushare_primary' THEN 2 ELSE 9 END,
                  available_at DESC) t
 WHERE b.symbol = t.symbol
   AND b.trading_date = t.trading_date
   AND b.trading_date BETWEEN CAST(:'from_date' AS date) AND CAST(:'to_date' AS date)
   AND b.adj_factor IS DISTINCT FROM t.adj_factor;
COMMIT;
```

### 步骤 4 —— 可选：标注占位证据（不要删除）

只有在 `app/annual_daily_backfill.py` 已排除这些行之后才有意义；标注让那条排除既便宜又显式。

```sql
BEGIN;
UPDATE quant.daily_adjustment_factors
   SET raw = raw || jsonb_build_object('superseded_at', now()::text,
                                       'superseded_reason', 'same_day_identity_only placeholder is never promoted to bar tables')
 WHERE provider = 'longhuvip_composite'
   AND raw->>'factor_semantics' = 'same_day_identity_only'
   AND raw->>'superseded_at' IS NULL;
-- 预期：UPDATE 61600
COMMIT;
```

### 步骤 5 —— 校验（只读，修复前后各跑一次）

09-04 ~ 09-18 的 `eq1` 列应从每天 ~5,1xx 降到 tushare 原生基线
（已回填日期约 130-142/天，未回填日期为 0），与健康的 09-02（134）、09-03（135）对齐。

```sql
SELECT b.trading_date,
       count(*)::bigint AS rows,
       count(*) FILTER (WHERE b.adj_factor IS NULL)::bigint AS null_factor,
       count(*) FILTER (WHERE b.adj_factor = 1)::bigint AS eq1,
       count(*) FILTER (WHERE b.adj_factor IS NOT NULL AND b.adj_factor <> 1)::bigint AS real_factor
  FROM quant.canonical_bars_daily b
 WHERE b.trading_date BETWEEN CAST(:'from_date' AS date) AND CAST(:'to_date' AS date)
 GROUP BY 1 ORDER BY 1;
```

再跑一次 `python scripts/equity-readiness.py --date 2026-09-18`（只读），
确认新契约报告 `state='ready'` 且 `adjustment_state='pending'`，而不是 `state='blocked'`。

### 步骤 6 —— 守护查询（CI / 每次发布前，必须返回 0）

判定条件已经**放宽**为"这张 bar 带着因子，存在因子证据，但**没有任何一行可提升的证据**"，
而不是原来的"存在那一个 `same_day_identity_only` 标记"。一个不写标记的厂商占位
原本对这条守护查询是隐形的，现在同样会被抓到。

```sql
SELECT count(*)::bigint AS identity_leaks
  FROM quant.canonical_bars_daily bar
 WHERE bar.adj_factor IS NOT NULL
   AND EXISTS (SELECT 1 FROM quant.daily_adjustment_factors evidence
                WHERE evidence.symbol = bar.symbol AND evidence.trading_date = bar.trading_date)
   AND NOT EXISTS (SELECT 1 FROM quant.daily_adjustment_factors promotable
                WHERE promotable.symbol = bar.symbol AND promotable.trading_date = bar.trading_date
                  AND promotable.provider LIKE 'tushare%'
                  AND coalesce(promotable.raw->>'factor_semantics','')
                      IN ('','corporate_action_cumulative'));
-- 修复前返回 35573（放宽前后在当前生产库上同为 35573，只读实测 2026-09-19；
-- market_bars_daily 两者同为 0）；步骤 1 之后必须是 0，并永远保持 0。
-- 同一条 SQL：app/adjustment_factor_maintenance.identity_factor_leak_sql('canonical_bars_daily')
-- market_bars_daily 用同一函数换表名即可。
```

---

## 5. 自动触发：两个入口

修复上线后，longhu 晚上没有任何厂商提供公司行为历史，因此**必须有自动触发**，
否则每个新交易日的 `adj_factor` 永久为 `NULL`，整个复权价栈天天失败关闭而无人补救。
这里有两个入口，职责不同：

### 5.1 盘后非门控阶段 `adjustment_factors`

`POST_CLOSE_STAGE_ORDER` 里排在 `full_market_daily` → `core_daily_controls` **之后**
（因子抓取要先有当日结算截面），执行 `app.main.sync_adjustment_factors_post_close()`
→ `adjustment_factor_maintenance.post_close_sync()`。

**回看窗口必须活得比退休计数器久。** 计数单位是"台账日"，而一个日期只有还在回看窗口里
才会被再次尝试、才会再记一天。旧的 `POST_CLOSE_LOOKBACK_DAYS = 5`（**日历日**）在真实
排程下永远凑不满 5 天：只有 post-close 这一个任务被装了（`-Daily -At 16:40` + 每
`RetryIntervalMinutes` 重复到约 22:40），而 `run-post-close-pipeline.ps1` 周六周日直接
`skipped`，所以周二被拒的日期只经过周二~周五 4 个晚上就掉出窗口，`MAX_CONSECUTIVE_BLOCKED_RUNS = 5`
不可达，这个日期每晚被重试到天荒地老。现在窗口这样算（`post_close_lookback_days()`，纯函数）：

- `POST_CLOSE_LOOKBACK_SESSIONS = MAX_CONSECUTIVE_BLOCKED_RUNS + 1 = 6`
  **个交易日**（多一个晚上的余量），用 `quant.market_trade_calendar` 里
  `is_open` 的 `DISTINCT calendar_date` 倒数第 6 个作为窗口起点，
  所以春节/国庆那种连休九天的周不会把日期挤出窗口；
- 下限 `POST_CLOSE_LOOKBACK_DAYS = 14` **日历日**：日历没回填、日历答不满 6 个交易日、
  或日历行比今天还新时一律取它。按装好的排程，5 个"车道晚上"最长跨到周五 + 6 天
  （周五被拒 → 下周四第 5 晚），再留一个晚上余量是 8 天，14 天是这个数加上假期余量；
- 两者取**大**，窗口只会变宽不会变窄。窗口解析结果写进回执的 `lookback_days`。
- `post_close_sync(lookback_days=...)` 显式传值时不查日历（给测试和一次性排障用）。

**它不判断任何东西。** 具体保证：

- 它在 `post_close_refresh.NON_GATING_STAGES` 里，所以 `run_refresh` 计算
  `deferred_stages` 时把它排除 → 它 `blocked`/`failed` **不会**把整轮变成 `partial`；
  仍然出现在 `stages` 里，并另外列进 `non_gating_stages_needing_attention`。
- 它**不在** `POST_CLOSE_STAGE_DEPENDENCIES` 的任何一边：既不被谁阻塞，也不阻塞谁。
- 它与 `controls_ready` 无关（那只看 `core_daily_controls`）。

**阶段状态 ≠ 车道状态。** 盘后任务带 repetition（约 16:40–22:40 每隔
`RetryIntervalMinutes` 重发同一个 `trade_date`），而
`post_close_refresh.record_stage_with_receipt` 会跳过 `quant.automation_runs` 里已
`completed` 的阶段，并且把它**不认识的状态（含 `skipped` / `unchanged`）一律归一成
`completed`**。所以 `post_close_sync()` 上报的 `status` 是
`post_close_stage_receipt()` 把车道结论翻译成回执词汇后的结果，原始结论另存
`lane_status`：

| 车道结论 | 阶段 `status` | 回执是否落定 |
|---|---|---|
| `completed`（没有留下任何日期） | `completed` | 是 |
| `unchanged`（本来就没有待补日期） | `unchanged` → 归一成 `completed` | 是 |
| `completed` 但 `skipped_dates > 0`（混合轮） | `blocked` | 否 |
| `skipped`（覆盖率闸门拒绝） | `blocked` | 否 |
| `failed` | `failed` | 否 |
| 其它（如 `planned`） | `blocked` | 否 |

`blocked`/`failed` 都是 `record_stage_with_receipt` 认识的状态，`start_or_resume_run`
只对 `completed` 保留回执，所以**一个因覆盖率跳过的因子轮会在当晚后续 repetition 里
重新评估**，而不是被一张"已完成"的回执封死一整晚。payload 里另有 `retryable` 与
`unrepaired_dates`，回执 `reason` 直接写明还欠哪几天。

理由：复权因子是另一条 provider 路线，它的可用性绝不能拖慢或拖垮晚间收盘流水线；
但"今晚就补一次"能让绝大多数交易日在当晚就拿到真因子。

### 5.1b 同日重试的 `skipped` 分支也必须跑这条车道

`scripts/windows/run-post-close-pipeline.ps1` 在"当日行情、策略与全部报告文件都已核对"
时直接 `return` 一条 `status='skipped'` 的记录。这条分支**在市场刷新之前就返回**，
而 5.1 那个非门控阶段的**唯一**调用方就是市场刷新
（`POST /api/v1/market/post-close/refresh` → `post_close_refresh_service`）。
结果是最顺利的那种晚上——入库与发布第一次就成功——反而**整晚碰不到因子车道**，
`adj_factor` 要等到次日 04:30 的维护任务才被补上，中间所有跨日研究窗口都只能靠
第 1.1 节的顺延来回答。

现在该分支在写 `skipped` 记录**之前**先调 `Invoke-AdjustmentFactorLane`：

- 入口是**真实的那一个**：`app/main.py` 只在 post-close refresh 内部暴露
  `sync_adjustment_factors_post_close()`，没有单独的 HTTP 端点，所以这里用
  04:30 任务用的同一个 CLI（本仓库 venv 的 python 调
  `scripts/adjustment-factor-maintenance.py sync --lookback-days N --env-file <path>`）。
  runtime.env 仍然只以**路径**交给 Python，PowerShell 侧不读出任何凭据值。
- 调用前清掉继承来的 `http_proxy`/`https_proxy`/`all_proxy`（理由同 5.2），
  退出后在 `finally` 里原样还原。
- 窗口默认 1 天（刚收盘的那个交易日）；用 `-TradeDate` 回补更早日期时按
  `今天 - 交易日 + 1` 放宽，上限 14 天，因为车道的工作清单是从 `china_today()`
  往回算的，1 天的窗口根本不包含那个日期。
- **它绝不改变 skip 判定**：整个函数包在 `try/catch` 里，任何失败都归为
  `factor_lane.status='error'` 并带上原因，记录进同一条 `skipped` 记录的
  `factor_lane`（`status` / `fetched` / `skipped` / `lookback_days` / `reason`）。
  一条挂掉的 provider 路线是 04:30 那个欠账负责人的事，绝不是重跑一个已核对完毕
  的交易日的理由。
- 契约测试：`scripts/windows/tests/test-post-close-pipeline-contract.ps1`
  （纯静态：断言该分支在写 `skipped` 记录前调用车道、车道调的是真实入口、
  失败被 `catch` 成回执而不是抛出、且 skip 判定从不读车道结论）。

### 5.2 计划任务 `trading-hareness-adjustment-factors`（每天 04:30）

```
pwsh scripts\windows\install-adjustment-factor-task.ps1
# 默认 RepositoryRoot/HostRoot = G:\StockPlatform\current，LookbackDays = 30
```

- `scripts/windows/run-adjustment-factor-maintenance.ps1`：先清掉继承来的
  `http_proxy`/`https_proxy`/`all_proxy`（桌面代理会把一条能用的 longhu 路线
  变成每晚的假失败），再用**当前 venv** 的 python 调
  `scripts/adjustment-factor-maintenance.py sync --lookback-days 30 --env-file ...`，
  逐行写入 `G:\StockPlatform\logs\adjustment-factors\<date>.log`，并透传退出码。
  runtime.env 只以**路径**交给 Python，PowerShell 侧从不读出任何凭据值。
- 安装器沿用其它任务一样的隐藏启动器（`New-HiddenPowerShellTaskAction`
  + `stock-background-host.exe`），`-Hidden -MultipleInstances IgnoreNew`，
  执行时限 45 分钟，**没有 restart-on-failure、没有 repetition**：工作清单每次
  都从真实因子覆盖率重算，漏跑一晚下一晚自然补上；重试一条挂掉的 provider
  只会成倍增加请求。
- 契约测试：`scripts/windows/tests/test-adjustment-factor-task-contract.ps1`
  （不注册任务、不连库、不发请求）。

### 5.3 CLI 与退出码

```
python scripts/adjustment-factor-maintenance.py sync \
    --lookback-days 30 \
    --env-file G:\StockPlatform\config\runtime.env \
    [--dry-run]
```

- `pending_dates()`：回看窗口内、`quant.market_trade_calendar` 判定为开市日、
  `quality_status IN ('fresh','partial')` 的结算 bar 中，
  **在 `quant.daily_adjustment_factors` 里有真实累计因子**的比例低于
  `ceil(0.95 * count(*))` 的交易日。"真实"用的是和写入方同一条正向规则
  （`REAL_FACTOR_PREDICATE_SQL` = `promotable_factor_evidence_sql()`：`longhu_qfq_derived` 且显式累计语义，
  或 provider 以 `tushare` 开头，且语义缺省或
  `corporate_action_cumulative`），不是"不等于那一个占位标记"——否则一个
  不写标记的厂商占位同样能冒充覆盖。阈值复用
  `daily_control_plane.MINIMUM_ALL_A_COVERAGE_RATIO`。
  判定问的是因子表而不是 bar 上的 `adj_factor`，所以修复前后都成立。
- 每个待补日期交给 `repair_factor_date(session, date)`（2026-09-19 前是
  `full_market_daily_controls_sync.sync(date, apis=('adj_factor',))` 的 tushare 拉取）：整轮只读一次
  窗口、抓一次 longhu、推导一次（`FactorLaneSession`），再按日期逐个事务写入（第 0.4 节）。
  单日失败不终止整轮。每个日期归为三种 `outcome` 之一：

  | outcome | 触发条件 | 影响 |
  |---|---|---|
  | `completed` | 推导并写入成功 | 清掉该日期的"连续被拒"计数 |
  | `skipped` | `blocked` 且 `blocked_by='coverage'`（当日日线截面不够 95%，`daily_row_count()` 为 0） | **不算失败**，带 `reason` 上报 |
  | `failed` | 抛异常，或 `blocked_by='provider'`（longhu 抓取失败超过 5% 的票，或推导覆盖不足当日 95%） | 整轮 `status='failed'` |

- **退出码：只有 `status='failed'` 才返回 1**，其余（`completed` / `planned` /
  `unchanged` / `skipped`）返回 0。原因：这条车道修不了别人家的日线截面，
  一个永远过不了覆盖率闸门的日期会让计划任务**每晚**报错，报到没人再看。
- **连续被拒 5 个"台账日"后退出清单**：每个被覆盖率拒绝的日期在
  `quant.automation_runs`（`task_key='adjustment_factor_maintenance.blocked_date'`，
  `run_key='adjustment-factor-blocked:<date>'`）累计 `consecutive_blocked_runs`。
  **计数单位是天，不是调用次数。** 台账的键是 `(交易日, 被拒当天)`：同一行里
  `output_summary.blocked_days` 是这个交易日被拒过的"台账日"集合，`consecutive_blocked_runs`
  就是它的大小；`_RECORD_BLOCKED_DATE_SQL` 在 `ON CONFLICT` 分支里先问
  `blocked_days @> 今天` 再决定要不要 +1，所以同一晚跑多少次都只算一次（判定在**同一条
  语句内**，两个并发调用也不会各加一次）。
  一个"台账日"从 CST **12:00** 起算（`BLOCKED_LEDGER_DAY_BOUNDARY_HOUR`）：
  post-close 流水线 15:30→22:40 每 `RetryIntervalMinutes` 跑一遍（一晚上十几次）、
  凌晨 04:30 维护任务再扫同一批欠账，这两者属于**同一个**台账日。
  没有这条键，`MAX_CONSECUTIVE_BLOCKED_RUNS = 5` 等于两个半小时而不是五天——
  一个交易日会在**当晚**就被退休，之后车道报 `unchanged`、回执落定，
  重试机制把自己关掉。`blocked_ledger_day()` 是这条规则的 Python 版本（给报表和测试用），
  真正算数的是 SQL；两者由 `BlockedDateLedgerPostgresTests` 在真 PostgreSQL 上对齐。
  **没有 `PGHOST` 时那条语句不会被执行**：默认跑的用例里计数决策来自测试文件里的
  `_BlockedDateLedger`（手写孪生），语句本身只由
  `test_the_statement_shape_keys_the_counter_on_the_day_not_the_invocation` 做**形状**校验——
  它用模块自己的片段拼出两个 CASE 分支的完整正文来断言（`THEN 计数 ELSE 计数+1`、
  `THEN 旧数组 ELSE 旧数组 || 今天`），所以把两个分支对调（即恢复"每次调用都 +1"的缺陷）
  会当场失败；`test_an_inverted_case_fails_the_shape_test` 就是这条负控本身。
  升级不丢账：老行没有 `blocked_days`，读作空数组，计数原样保留，下一个台账日才 +1。
  达到 `MAX_CONSECUTIVE_BLOCKED_RUNS = 5` 时**写一次**
  `quant.data_quality_issues` 回执（`code='adjustment_factor_date_retired'`）
  并从此不再出现在工作清单里（`sync()` 结果的 `retired_dates`）。之后再被拒不再重复
  告警。回执消息里的天数与日期清单**只说它能证明的**（`_blocked_days_receipt_phrase()`）：
  两者一致时直接列出那 5 个被拒日；升级行的计数来自"按调用计"的旧时代、日期清单只有
  新记的那几天，此时写成 `recorded days: <日期>；更早的那些早于这条键，是按调用计的`，
  不会把"5 天"和"1 个日期"并排印成自相矛盾的一句。任何一次抓取成功都会把计数与 `blocked_days`
  一起清零，该日期重新回到清单、并从下一个台账日重新数起，同时
  `clear_blocked_date()` 在同一个事务里把那条 `adjustment_factor_date_retired`
  回执置 `resolved_at=now()`——否则它是一条**永远无人能关**的告警。
  要手动让一个日期回到清单：先修当天的日线覆盖率，再删掉那一行 `automation_runs`
  （或等下一次成功抓取自动清零）。
  退休期间它对使用者是可见的，而且**三个界面用同一个词**：`pending_dates_between()`
  默认把退休日期从"待补"里剔除；个股窗口就绪度对该日期报 `retired` 并给出 `run_key`
  与被拒原因（第 2 节第 12 项）；日控制面 `status_payload` 也报
  `adjustment_state='retired'`（第 2 节第 8 项），而不是继续说"已排队"。
- `--dry-run` 只解析并打印待处理日期，不发 provider 请求、不写库。
- stdout 是 ASCII-only JSON（任务宿主控制台是 GBK）；env 文件只写进 `os.environ`，
  任何凭据值都不会被打印或落盘。

### 5.4 `status` 子命令（只读报告）

```
python scripts/adjustment-factor-maintenance.py status \
    --lookback-days 14 \
    --env-file G:\StockPlatform\config\runtime.env
```

只读：不发 provider 请求、不写库、没有自己的退出码语义（永远 0）。
"这条车道现在到底处在什么状态"以前要靠手写 SQL 拼出来，于是每个人拼的定义都略有不同；
这个子命令把**和车道完全相同的那些定义**印出来
（`REAL_FACTOR_PREDICATE_SQL` 算覆盖、`pending_and_retired_dates_between()` 算工作清单与
退休台账、`identity_factor_leak_sql()` 就是发布守护查询本身）。

输出是一个 ASCII-only JSON 文档（`app.adjustment_factor_maintenance.status_report()`）：

| 字段 | 内容 |
|---|---|
| `dates[]` | 窗口内每个开市结算日的 `daily_rows` / `adjustment_rows`（真实因子：tushare 历史行或 `longhu_qfq_derived`）/ `coverage_ratio` / `pending` / `retired` |
| `dates[].retired` | 已退休时带 `run_key`（要清的那一行）、`reason`、`blocked_days`、`consecutive_blocked_runs` |
| `pending_dates` / `retired_dates` | 与车道工作清单同源的两份清单 |
| `identity_factor_leaks` | 第 4 节步骤 6 的守护查询，**不带窗口**（它是发布门槛，必须全表为 0；带窗口的版本会在污染落在窗口外时假装通过），两张 bar 表各一个数 |
| `factor_fetch_runs[]` | 最近几条 `capability='adj_factor'` 的 `quant.fetch_runs`：provider / status / row_count / 时间 / `error_class`。**不含 `last_error` 正文**——那是形状不受控的 provider 文本，这份报告会被贴进工单 |
| `provider_capability` | `adjustment_factor_maintenance.longhu_factor_route()`：唯一路线 `longhu_qfq_derived`，`status='verified'` 只表示本机配置了 longhu 授权源；因子是否真的落地看 `factor_fetch_runs` 与逐日覆盖（2026-09-19 前读的是 tushare 的 capability registry） |
| `provider_health[]` | `quant.provider_health` 里该 capability 的每条路线：连续失败数、熔断是否打开、最近成功/失败时间 |
| `post_close_stage_receipt` | 最近一条非门控盘后阶段回执（`task_key='post_close_refresh.stage'`、`run_key LIKE '%:adjustment_factors:%'`），只摘 `status` / `lane_status` / `retryable` / `unrepaired_dates` / `lookback_days` / `reason` |
| `summary` | 一行人读的小结（纯函数 `status_summary()`，由单测钉住，不会和上面的数字走散） |

> 2026-09-19 只读实测（生产库，修复 SQL 尚未执行）：
> `2026-09-05..2026-09-19: 10 settled date(s), 4 complete, 6 pending, 0 retired;`
> `identity factor leaks 35573; last fetch tushare_primary blocked rows=0 at`
> `2026-09-18 15:29:36+08:00; route available (super_get, super, primary);`
> `no post-close factor-stage receipt` —— 守护值 35,573 与第 4 节步骤 6 的记录一致。

---

## 6. 修复后的预期影响（必须提前通知使用者）

### 不会出现全市场黑屏（第 1.1 节的末尾缺口顺延）

**最初的设计是"只要窗口里有一个 `NULL` 日就整窗失败关闭"。那一版会在因子车道
落后的每一个晚上把全市场一起黑掉，误伤与个股无关 —— 现在不再是那样。**

因子车道晚一两天时（最常见的情况：longhu 收盘、当晚 `adjustment_factors` 还没成功），
`NULL` 落在窗口**末尾**、缺口 ≤ 5 个交易日、且缺口内每天 `pre_close` 与前收连续，
则窗口按最后一个真实因子顺延计算，只带一个 `adj_factor_carried_forward` 标记：

- `app/feature_snapshot_repository.py`：`research_price_status='carried_forward'`，
  并给出 `research_adj_factor_carried_sessions`；sma/return 照常有值
- `app/watchlist_daily_factors.py`、`app/post_close_structures.py`（30/15 日窗口）：
  `status` 照常产出，`quality_flags` 带 `adj_factor_carried_forward`
- `app/ten_day_leader_ranking.py`：个股不再被剔除，
  `source_status.carried_forward_factor_symbols` 记录有多少只走了顺延
- `app/effectiveness/execution.py`：`simulated`，回执带
  `adjustment_basis='carried_forward'` 与 `carried_factor_sessions`
- `app/watchlist_main_wave.py`：末尾 `NULL` 的 bar 不再被丢弃，当前行 payload 带
  `quality_flags=['adj_factor_carried_forward']`，整轮带
  `metrics.carried_forward_factor_symbols`

**仍然失败关闭的只有三种**（这才是真正该报警的）：

1. 缺口内出现除权迹象（`pre_close ≠ 前收`）→ `corporate_action_unresolved`；
2. 末尾缺口超过 5 个交易日，或窗口中间有洞、或整窗无真实因子 → `adj_factor_missing`
   （`feature_snapshot` 仍为 `research_price_status='blocked'`，
   `watchlist_daily_factors` / `post_close_structures` 仍为 `data_quality_blocked`）；
3. 窗口里没有 `pre_close` 可比 → 同上 `adj_factor_missing`。

未纳入顺延、行为不变的两处：`app/factor_lab.py`（逐行接口，跨日比值仍为 `None`）与
`app/factor_sql_lab.py`（`WHERE bar.adj_factor>0` 在 SQL 里排除 `NULL` 行）。

步骤 2/3 回填后，已回填日期立即回到"真实因子"路径；顺延只是给车道留出最多 5 天，
**不是**替代补齐 —— 超过 5 天仍然黑屏，就是在催维护任务。

### 打分：顺延不扣分，缺失照扣

`app/recommendation_generation.py` 把 `adj_factor_missing` 与
`corporate_action_unresolved` 列为硬标记（降级为 `watch`，每个标记扣 0.07、上限 0.35）。
`adj_factor_carried_forward` **既不是硬标记也不参与扣分计数**
（`UNPENALIZED_FLAGS`），因为它描述的是平台抓取状态而不是个股缺陷；扣它等于在
车道落后的晚上把整个候选池一起压低，本来更好的股票会因此输给一只"恰好窗口完整"的。
它仍然写进 `risk_flags`，读的人能看到这一票是按顺延基准算的。

因此修复后**不存在**原先预告的"全市场打分悬崖"：只有真正除权未建模、或缺口超过
5 天的个股才会下沉。这条由
`tests/test_recommendation_carried_factor_scoring.py` 钉住。

### 已落库的错误结论不会被这次 SQL 修复

`app/effectiveness/execution.py` 过去对完全落在 09-04 ~ 09-18 内的窗口看到的是恒定 1.0，
于是用 **原始价格** 模拟出了 `net_return_pct` / `adverse_excursion_pct`；跨 09-03 → 09-04 的
窗口则被错误地判为 `corporate_action_unmodeled`。这些结论已经通过
`app/short_term_lanes/tracking_repository.py` 写入 `quant.strategy_observation_evaluations`，
修复 bar 表不会重算它们 —— 需要单独的重放。

### 其他已知风险

- **归档污染**：`scripts/marketdata/export_pg_to_parquet.py` 按 `trading_date` 导出
  `daily_adjustment_factors`。2026-09-01 到修复之间做的任何 parquet 归档都带着
  61,600 行占位，且只有 `raw->>'factor_semantics'` 能区分。任何只按 provider 过滤的
  冷层回放都会把它们重新引入。
- **基准混淆**：tushare 的 `adj_factor` 是累计（后复权）因子；
  `quant.research_adjusted_bars_daily` 是另一个 provider（`stock_brain_tencent_qfq`）的
  前复权序列，两者绝不可比较或互相替代；而且它只到 2026-09-01，无法覆盖受损窗口。
- **覆盖率闸门错配**：`full_market_daily_controls_sync.sync()` 要求截面覆盖 ≥95% 的
  `daily_row_count()`，后者本身在 canonical 覆盖率不足 95% 时返回 0。longhu 日的 canonical
  截面约 5,130，all_a（已排除 BJ）期望约 5,248，通常能过；但若某天不过，
  只跑 adj_factor 的任务会在开始前就被拒绝并报 `blocked`，`blocked_by='coverage'`。
  这类日期现在**记为 `skipped` 而不是失败**（退出码仍是 0），连续 5 次被拒后带一次性
  回执退出工作清单（见 5.3）。**届时该修的仍然是当日 daily 覆盖**：这条车道修不了它，
  退出清单只是让告警不再每晚重复，不是把问题解决掉。
