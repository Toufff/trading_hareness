# 复权因子语义与一次性修复手册（2026-09-19）

本文件是 `adj_factor` 语义的权威说明，以及 2026-09-01 ~ 2026-09-18 占位因子污染的
一次性修复流程。**本分支没有执行过任何修复 SQL，也没有对生产库做任何写入。**
下面的 SQL 是给运维步骤用的脚本，必须按第 3 节的顺序在发布之后执行。

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
| 真实累计因子（> 0） | 已取到 tushare 累计因子 | 正常复权 |
| `NULL` | 尚未取到（待补） | 失败关闭：`adj_factor_missing` / `data_quality_blocked` / `adjustment_pending` |
| `1`（伪造占位） | **禁止** | 静默产生未复权序列 |

> `1` 本身并不一定是错的：新上市个股确有合法的 `adj_factor = 1`。判定范围的谓词
> 永远是"该 symbol+date 存在 `same_day_identity_only` 占位证据且没有任何
> tushare 行"，**不是** `adj_factor = 1`。2026-09-04 之前有 96,134 行合法的 1.0。

---

## 2. 本次代码改动（已包含在本分支）

1. `app/longhu_market_sync.py` `build_control_rows()`：不再生成 `adj_factor` 行，
   返回值只有 `{"stk_limit": ...}`。
2. `app/longhu_market_repository.py`：不再 `persist_rows(..., "adj_factor", ...)`；
   `fetch_runs` 回执里的 `control_semantics.adj_factor` 改为
   `not_supplied_by_vendor; fetched separately from tushare adj_factor`。
3. `app/tushare_normalization.py`：因子行写入 `daily_adjustment_factors` 仍保留为证据，
   但只有 `promotable_adjustment_factor(row)` 为真（未声明 `factor_semantics`，
   或声明为 `corporate_action_cumulative`）才会 `UPDATE canonical_bars_daily`。
   这是"按语义"而不是"按 provider 白名单"的防御，未来任何厂商占位都会被构造性拦住。
4. `app/daily_control_plane.py` `_longhu_control_status()`：短路门槛按控制项拆分，
   去掉 `factor_rows` 项，回执带 `satisfied_by_vendor` / `pending_controls` /
   `adjustment_state`。
5. `app/full_market_daily_controls_sync.py`：`sync()` 新增 `apis` 参数；两条
   `is_suspended=false` 复位、涨跌停回填、停牌回填、复权回填分别按各自 API 是否在
   `apis` 中做闸门。**只跑 adj_factor 的任务绝不会清掉当日停牌标记。**
6. `app/adjustment_factor_maintenance.py`（新）：`pending_dates()` / `sync()`，
   对覆盖率不足的交易日以 `apis=('adj_factor',)` 逐日补取。
7. `app/capability_registry.py`：`adj_factor` 独立分支，
   `preferred_providers=('super_get','super','primary')`、`status='verified'`
   （`tushare_primary` 的 adj_factor 当前是 `failed`/ConnectError）。
8. `app/daily_control_plane.py` `status_payload()`：`limits_ready` 与三态
   `adjustment_state ∈ {complete, pending, absent}` 拆开；
   **`ready` 不再包含复权项**；新增 `adjustment_state`、`adjustment_pending_rows`、
   `research_adjustment_ready`。
9. `app/ten_day_leader_rotation_repository.py`：两处覆盖度判定去掉
   `adj_factor IS NOT NULL`（改由 `quality_status` 回答"是否有结算 bar"）。
10. `app/effectiveness/execution.py` `simulate()`：`adj_factor` 移出必填字段，
    `NULL` 返回 `adjustment_pending`，只有全部有因子且不一致才是
    `corporate_action_unmodeled`。
11. `app/annual_daily_backfill.py` `reconcile_suspensions()`：候选行排除
    `raw->>'factor_semantics' = 'same_day_identity_only'`。**没有这条，下面的修复不持久。**
12. `app/stock_study_readiness_repository.py`：`adj_factor` 条目新增 `note`，
    区分 `pending`（等待补取）与 `missing`（从未取过）。
13. `scripts/adjustment-factor-maintenance.py`：
    `sync --lookback-days N --env-file ... --dry-run`，ASCII-only stdout。

---

## 3. 执行顺序（必须严格按此顺序）

> **顺序就是全部风险。** 第 4 节的 NULL 化一旦先于第 8/9 项代码上线，
> 每个 longhu 交易日的 `status_payload` 立刻变成 `state='blocked'`，
> 十日龙头轮动车道的覆盖日期 CTE 会返回空集，整条车道静默停产。

1. **发布**：把本分支合入并发布，让第 8 项（readiness 契约）与第 9 项
   （十日龙头谓词）**同一次发布**生效。发布后先跑一次只读校验：
   `python scripts/equity-readiness.py --date 2026-09-18`
   应当仍是 `state='ready'`（此时复权行还是占位的 1，`adjustment_state='complete'`）。
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

## 4. 修复 SQL

> 范围从 **2026-09-01** 开始，不是 09-04。09-01 同样是 longhu 占位日（5,235 行受污染的
> canonical 行 / 5,266 行 NULL 的 market 行），漏掉它会在每个 60 日窗口中间留下一天未复权空洞，
> 而且它是最便宜的一天：`tushare_primary` 已经有当天 5,567 只的完整截面。

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
 WHERE b.trading_date BETWEEN DATE '2026-09-01' AND DATE '2026-09-18'
   AND b.adj_factor = 1
 GROUP BY 1 ORDER BY 1;
```

### 步骤 1 —— NULL 化（只有占位证据的行）

`NULL` = "没有复权信息"，正是 `app/research_prices.py` 的 `adjusted_bars()` 设计要识别的状态。

```sql
BEGIN;
UPDATE quant.canonical_bars_daily b
   SET adj_factor = NULL, canonicalized_at = now()
 WHERE b.trading_date BETWEEN DATE '2026-09-01' AND DATE '2026-09-18'
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
         WHERE trading_date BETWEEN DATE '2026-09-01' AND DATE '2026-09-18'
           AND provider LIKE 'tushare%'
           AND coalesce(raw->>'factor_semantics','') <> 'same_day_identity_only'
         ORDER BY symbol, trading_date,
                  CASE provider WHEN 'tushare_super_sdk' THEN 0 WHEN 'tushare_super_get' THEN 1
                                WHEN 'tushare_primary' THEN 2 ELSE 9 END,
                  available_at DESC) t
 WHERE b.symbol = t.symbol
   AND b.trading_date = t.trading_date
   AND b.trading_date BETWEEN DATE '2026-09-01' AND DATE '2026-09-18'
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
         WHERE trading_date BETWEEN DATE '2026-09-01' AND DATE '2026-09-18'
           AND provider LIKE 'tushare%'
           AND coalesce(raw->>'factor_semantics','') <> 'same_day_identity_only'
         ORDER BY symbol, trading_date,
                  CASE provider WHEN 'tushare_super_sdk' THEN 0 WHEN 'tushare_super_get' THEN 1
                                WHEN 'tushare_primary' THEN 2 ELSE 9 END,
                  available_at DESC) t
 WHERE b.symbol = t.symbol
   AND b.trading_date = t.trading_date
   AND b.trading_date BETWEEN DATE '2026-09-01' AND DATE '2026-09-18'
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
 WHERE b.trading_date BETWEEN DATE '2026-09-01' AND DATE '2026-09-18'
 GROUP BY 1 ORDER BY 1;
```

再跑一次 `python scripts/equity-readiness.py --date 2026-09-18`（只读），
确认新契约报告 `state='ready'` 且 `adjustment_state='pending'`，而不是 `state='blocked'`。

### 步骤 6 —— 守护查询（CI / 每次发布前，必须返回 0）

```sql
SELECT count(*)::bigint AS identity_leaks
  FROM quant.canonical_bars_daily b
 WHERE b.adj_factor IS NOT NULL
   AND EXISTS (SELECT 1 FROM quant.daily_adjustment_factors ident
                WHERE ident.symbol = b.symbol AND ident.trading_date = b.trading_date
                  AND ident.raw->>'factor_semantics' = 'same_day_identity_only')
   AND NOT EXISTS (SELECT 1 FROM quant.daily_adjustment_factors f
                WHERE f.symbol = b.symbol AND f.trading_date = b.trading_date
                  AND f.provider LIKE 'tushare%');
-- 修复前返回 35573；步骤 1 之后必须是 0，并永远保持 0。
-- 同一条 SQL：app/adjustment_factor_maintenance.identity_factor_leak_sql('canonical_bars_daily')
-- market_bars_daily 用同一函数换表名即可。
```

---

## 5. 维护任务：`scripts/adjustment-factor-maintenance.py`

```
python scripts/adjustment-factor-maintenance.py sync \
    --lookback-days 30 \
    --env-file G:\StockPlatform\config\runtime.env \
    [--dry-run]
```

- **在盘后流水线之外运行**，属于 04:00-08:00 维护窗，以及本次一次性修复。
  复权因子来自另一个 provider，它的可用性绝不能拖慢或拖垮晚间收盘流水线。
- `pending_dates()`：回看窗口内、`quant.market_trade_calendar` 判定为开市日、
  `quality_status IN ('fresh','partial')` 的结算 bar 中，
  **在 `quant.daily_adjustment_factors` 里有真实 tushare 累计因子**（provider 以
  `tushare` 开头且 `factor_semantics` 不是 `same_day_identity_only`）的比例低于
  `ceil(0.95 * count(*))` 的交易日。阈值复用
  `daily_control_plane.MINIMUM_ALL_A_COVERAGE_RATIO`。
  判定问的是因子表而不是 bar 上的 `adj_factor`，所以修复前后都成立。
- 每个待补日期调用 `full_market_daily_controls_sync.sync(date, apis=('adj_factor',), ...)`，
  单日失败不终止整轮（`status` 变为 `partial`）。
- `--dry-run` 只解析并打印待处理日期，不发 provider 请求、不写库。
- stdout 是 ASCII-only JSON（任务宿主控制台是 GBK）；env 文件只写进 `os.environ`，
  任何凭据值都不会被打印或落盘。
- 退出码：`completed` / `planned` / `unchanged` → 0；`partial` → 1。

---

## 6. 修复后的预期影响（必须提前通知使用者）

### 特征黑屏窗口

因子变 `NULL` 之后，只要窗口里包含任意一个 `NULL` 日，所有 `adjusted_bars` 消费者都会
**失败关闭**（这是正确行为，但看起来像故障）：

- `app/feature_snapshot_repository.py`：`research_price_status='blocked'`，sma/return 全为 `None`
- `app/watchlist_daily_factors.py`：`data_quality_blocked`
- `app/post_close_structures.py`（30/15 日窗口）：`data_quality_blocked`
- `app/factor_lab.py`：跨日比值全部为 `None`
- `app/ten_day_leader_ranking.py`：缺因子的个股被逐只剔除
- `app/factor_sql_lab.py`：`WHERE bar.adj_factor>0` 自动把 `NULL` 行排除出源 CTE

步骤 2/3 回填后，已回填日期立即恢复；只有确实没有 tushare 因子的日期会持续黑屏，
直到维护任务补齐。

### 打分悬崖

`app/recommendation_generation.py` 把 `adj_factor_missing` 与
`corporate_action_unresolved` 列为硬标记，每个扣 0.07、上限 0.35。今天它们在 longhu 日
从不触发；修复后在因子补齐之前会对几乎所有候选触发，推荐分数会整体下沉。
**这是正确行为，但如果不提前通知，看起来就像系统故障。**

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
  只跑 adj_factor 的任务会在开始前就被拒绝并报 `blocked`。届时先修当日 daily 覆盖。
