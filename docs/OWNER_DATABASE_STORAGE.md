# 业主数据库存储分层（OWNER_DATABASE_STORAGE）

本文件是**业主 PostgreSQL 数据库物理布局的唯一权威文档**。改动数据目录、备份、
定时维护任务，或改动分层策略里列出的任何一张表之前，先读完本文。

- 适用范围：Windows 工作站上的 `trading_hareness` 集群（PostgreSQL 16.15，端口 `55432`）。
- 不适用：`intraday_edge`（47 机）与 parquet/百度网盘四层协议，那是
  [`DATA_TIERING_PROTOCOL.md`](DATA_TIERING_PROTOCOL.md) 的范围。两份文档互不覆盖：
  本文管"业主库的字节落在哪块盘上"，那份管"哪一类数据属于哪一层"。
- 相关实现：`scripts/database-storage-tiers.py`、`scripts/windows/run-storage-tiers.ps1`、
  `scripts/windows/set-postgres-io-window.ps1`、`scripts/windows/migrate-postgres-data-directory.ps1`、
  `scripts/windows/postgres-managed-config.psm1`、`scripts/windows/backup-stock-database.ps1`、
  `scripts/windows/stock-incremental-backup.psm1`、
  `quant-service/migrations/versions/20260919_0106_storage_tier_cutoff_indexes.py`。

---

## 1. 三层布局

| 层 | 物理位置 | 内容 | 容量约束 |
|---|---|---|---|
| **hot（热层）** | `F:\StockPlatformDB\postgresql16`（NVMe，由 `PGDATA_DIR` 决定） | 整个 PostgreSQL 集群：所有表、索引、WAL、临时文件 | **500 GB 软上限**（`PGDATA_BUDGET_BYTES`） |
| **cold（冷层）** | 表空间 `stock_cold` → `G:\StockPlatform\data\pg-cold`（HDD，由 `PGDATA_COLD_TABLESPACE_DIR` 决定） | `quant.<表>_cold` 孪生表（超出热窗的历史行）＋整张 `quant.legacy_source_records` | 无上限（G: 为 12 TB 机械盘） |
| **backup（备份）** | `G:\StockPlatform\backups` ＋ 百度网盘异地副本 | 每夜 `pg_dump` 自定义格式备份、增量分块、异地加密副本 | 沿用既有保留策略（日 14 天 ＋ 周 8 周） |

关键事实：

- **平台根目录仍在 G:。** 只有 PostgreSQL 热数据目录搬到了 F:。报告、备份、冷表空间、
  运行时二进制、`config\runtime.env` 全部留在 `G:\StockPlatform` 下，
  `start-stock-dashboard.ps1` 里"Authoritative stock data must remain on G:"这条断言因此保留，
  它校验的是平台根，不是数据目录。
- **热数据目录由 `PGDATA_DIR` 统一解析**，任何脚本都不得再硬编码
  `G:\StockPlatform\data\postgresql16`。解析逻辑集中在
  `scripts/windows/postgres-managed-config.psm1` 的 `Resolve-StockPlatformDataDirectory`
  （PowerShell 侧）与 `scripts/database-storage-tiers.py` 的 `resolve_settings`、
  `scripts/stock-platform-context.py` 的 `storage_layout`（Python 侧）。
  未配置时回退到旧的 G: 路径，老部署因此不受影响。
- **冷层不是归档，是在线数据。** 冷孪生表和 `quant.<表>_all` 视图都在同一个库里，
  随时可查，只是落在机械盘上，规划器按 `random_page_cost = 4` 对待它。

### runtime.env 新增键

这些键都可选，都有文档化默认值，写在 `G:\StockPlatform\config\runtime.env`：

| 键 | 默认值 | 含义 |
|---|---|---|
| `PGDATA_DIR` | `G:\StockPlatform\data\postgresql16` | 热数据目录；生产为 `F:\StockPlatformDB\postgresql16`。由 `initialize-stock-platform.ps1` 写入，由数据目录迁移脚本改写。 |
| `PGDATA_BUDGET_BYTES` | `536870912000`（500 GB） | 热层预算。仅在缺失时由初始化脚本补写（`Set-StockPlatformEnvDefault`），不会覆盖运维已调过的值。 |
| `PGDATA_COLD_TABLESPACE_DIR` | `G:\StockPlatform\data\pg-cold` | `stock_cold` 表空间目录。同样只补写不覆盖。 |
| `STOCK_BACKUP_EXCLUDE_TABLE_DATA` | **无默认值（初始化脚本刻意不补写）** | 纯运维覆盖项。每夜 dump 真正跳过哪些表的数据是**在 dump 时按增量链算出来的**，不是配置出来的；这个键只用于额外追加，且无增量链、或链已停滞的冷孪生表会被拒绝。见第 5 节。 |
| `STORAGE_TIER_HOT_DAYS` | `365` | 分层热窗口天数，**一个键驱动两侧**：`run-storage-tiers.ps1` 读它并把 `--hot-days` 传给分层 CLI（决定哪些行离开热表），`backup-stock-database.ps1` 读同一个键判断增量链水位线是否还追得上分层截止线（决定冷孪生能不能继续被 dump 排除）。所以改热窗口**只改这个键**即可，两侧自动一致；反过来，只在命令行上传 `--hot-days` 而不动这个键，备份侧仍按 365 天判"够新"，会排除一份链根本没导出过的冷孪生。键缺失时两侧都用 365；写成非正整数或非数字，两侧都拒绝运行而不是回退默认值。 |

分层作业还会读两个**属于备份链的**既有键，用来把搬运钳制在分块链水位线上（见 2.4 的"增量链钳位"）：

| 键 | 默认值 | 分层作业拿它做什么 |
|---|---|---|
| `STOCK_BACKUP_INCREMENTAL_TABLES` | `quant.raw_market_observations:created_at:updated_at` | 哪些热表的行由分块链带走。凡是列在这里的表，它的搬运会被钳制在链的水位线之前——正是这些表的冷孪生会被 dump 排除。解析由 `parse_incremental_specs` 完成，与 `Get-StockIncrementalTableSpecs` 逐字同构（含 `none` 形式），两边的默认值由 `test-postgres-storage-tier-wiring.ps1` 钉死成同一个字符串。 |
| `STOCK_BACKUP_ROOT` | `G:\StockPlatform\backups` | 到哪儿读 `incremental\<表>\state.json`。CLI 的 `--backup-root` 可以覆盖它（演练用，免得指到生产备份根上）。 |

---

## 2. 分层策略与空间策略

### 2.1 按时间分层的表

超过热窗的行被搬进 `quant.<表>_cold`；策略定义在
`scripts/database-storage-tiers.py` 的 `TIER_POLICY`，本表由
`quant-service/tests/test_storage_tier_policy.py` 校验与代码逐字一致。

<!-- tier-policy-table:begin -->
| 表 | 时间列 | 热窗（天） | 冷孪生表 | 联合视图 |
|---|---|---|---|---|
| `quant.raw_market_observations` | `available_at` | `365` | `quant.raw_market_observations_cold` | `quant.raw_market_observations_all` |
| `quant.tushare_raw_records` | `available_at` | `365` | `quant.tushare_raw_records_cold` | `quant.tushare_raw_records_all` |
| `quant.intraday_quote_observations` | `observed_at` | `365` | `quant.intraday_quote_observations_cold` | `quant.intraday_quote_observations_all` |
| `quant.intraday_rule_input_snapshots` | `observed_at` | `365` | `quant.intraday_rule_input_snapshots_cold` | `quant.intraday_rule_input_snapshots_all` |
| `quant.edge_evidence_changes` | `changed_at` | `365` | `quant.edge_evidence_changes_cold` | `quant.edge_evidence_changes_all` |
| `quant.trade_thesis_evaluations` | `created_at` | `365` | `quant.trade_thesis_evaluations_cold` | `quant.trade_thesis_evaluations_all` |
| `quant.factor_maintenance_changes` | `recorded_at` | `365` | `quant.factor_maintenance_changes_cold` | `quant.factor_maintenance_changes_all` |
<!-- tier-policy-table:end -->

热窗 365 天是**下限约束**，不是随手取的数字：正式验证需要 60 个交易日 ＋ 200 个成熟信号
（`POLICY_MIN_*`），证据保留因此至少一年（见 `DATA_TIERING_PROTOCOL.md` P4）。
缩窗等于销毁未来的验证资格，所以只有空间策略在濒临爆盘时才允许临时缩窗，且永不低于 30 天。

### 2.2 整表放冷层的表

没有孪生表，整张表（连同索引）直接 `ALTER TABLE ... SET TABLESPACE stock_cold`：

<!-- whole-table-cold:begin -->
| 表 | 理由 |
|---|---|
| `quant.legacy_source_records` | 2.6 GB 的旧 stock-brain 导入证据，只在追溯历史来源时读，从不在热路径 join |
<!-- whole-table-cold:end -->

### 2.3 冷孪生表是怎么造出来的

```sql
CREATE TABLE quant.<表>_cold
  (LIKE quant.<表> INCLUDING DEFAULTS INCLUDING INDEXES) TABLESPACE stock_cold;
-- 建表时 SET default_tablespace，并对残留索引 ALTER INDEX ... SET TABLESPACE，
-- 保证索引也落在冷盘上
-- 视图列出显式列名，绝不用 SELECT *：PostgreSQL 在建视图时就把 * 展开冻结成
-- 当时的列集，之后热表加一列，视图会永远少那一列，而且没有任何报错
CREATE OR REPLACE VIEW quant.<表>_all AS
  SELECT <显式列> FROM quant.<表> UNION ALL SELECT <显式列> FROM quant.<表>_cold;

-- 搬运时用来定位 cutoff 的索引，install 用 CONCURRENTLY 建（见 2.6）
CREATE INDEX CONCURRENTLY IF NOT EXISTS <表>_tier_cutoff_idx ON quant.<表> (<时间列>);
```

`LIKE` **不会**复制外键与触发器，这正是孪生表想要的：冷行不再参与引用完整性，
删除一个 `instruments` 行不应该级联删掉一年前的观测证据。
代价是冷层没有外键保护，因此有一条硬规则：

> **应用代码永远不得引用 `*_cold` / `*_all` 对象。** 它们是运维与追溯工具。
> 由 `quant-service/tests/test_storage_tier_policy.py` 强制。

#### schema 漂移（热表加了列怎么办）

热表由 Alembic 迁移演进，孪生表是 `install` 造出来的，两边会分叉。
`schema_drift()`（纯函数，已单测）按 `(列名, format_type)` 比对两侧：

| 情况 | `install` | `apply` |
|---|---|---|
| 冷表缺了热表新增的列（可修复） | `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`（可空，已有冷行本来就没有这个值），然后重建视图 | 正常搬运 |
| 冷表多一列，或同名列类型变了（不可修复） | 记 `schema_drift`，跳过该表的视图 | **拒绝搬这张表**，回执 `status='schema_drift'`，退出码 1 |

`CREATE OR REPLACE VIEW` 不允许改列集，所以列集变了时视图是 `DROP` 后重建；
`app/` 里没有任何东西依赖它（守卫测试保证），所以重建是安全的。

### 2.4 搬运机制

一批一个事务，**先写冷表、后按主键删热表**，五步全在同一个事务里：

```sql
-- 1) 冻结这一批（显式列名，不用 SELECT *；ORDER BY 时间列走 2.6 的 cutoff 索引）
--    FOR UPDATE 写在 ORDER BY / LIMIT 之后：这一批的行被锁住，直到本事务提交
CREATE TEMPORARY TABLE tier_batch ON COMMIT DROP AS
  SELECT <显式列> FROM quant.T WHERE <列> < %(cutoff)s
    [AND (<创建列> IS NULL OR <创建列> < %(chain_watermark)s)]   -- 见"增量链钳位"
  ORDER BY <列> LIMIT %(batch)s FOR UPDATE;

-- 2) 找出"自然键已经在冷表里、但内容不同"的行（逐个唯一键扫一遍）
CREATE TEMPORARY TABLE tier_conflicts ON COMMIT DROP AS
  SELECT DISTINCT ON (<主键>) <主键>, to_jsonb(b.*), to_jsonb(c.*)
  FROM tier_batch b JOIN quant.T_cold c ON <唯一键相等>
  WHERE to_jsonb(b.*) IS DISTINCT FROM to_jsonb(c.*);

-- 3) 把冲突行整行隔离（热行主键、整行热版本、整行冷版本）
INSERT INTO quant.storage_tier_conflicts (table_name, hot_pk, hot_row, cold_row) ...;

-- 4) 其余行写进冷表（显式列名）
INSERT INTO quant.T_cold (<显式列>) SELECT <显式列> FROM tier_batch b
  WHERE NOT EXISTS (SELECT 1 FROM tier_conflicts q WHERE <主键相等>) ON CONFLICT DO NOTHING;

-- 5) 按主键删热表 —— 只有在冷表已经拿到这些行之后
DELETE FROM quant.T h USING tier_batch b WHERE <主键相等>;
```

- **不存在"行既不在热表也不在冷表"的瞬间**：插入在删除之前，整批一个事务，
  任何时刻被 kill 都整批回滚。已用 4 万行、中途在 17 800 行处 kill 的真实演练验证过。
- **用主键定位，不用 `ctid`。** `ctid` 跨语句不稳定（`VACUUM`、HOT 更新都会挪行），
  而插入和删除现在是两条语句。五张分层表都有 uuid 主键；没有主键的表直接跳过，
  回执记 `skipped_no_primary_key`。
- 默认 `--batch 20000`；`statement_timeout` 每批 10 分钟（`--timeout-ms`），
  `lock_timeout` 30 秒（每条连接都带，不可关）。
- 幂等且可续跑：中断后重跑继续搬剩下的行；第 4 步的 `ON CONFLICT DO NOTHING`
  命中说明上一次跑到一半被打断（同键同内容），回执记 `already_in_cold_rows`，不是丢数据。
- **快照带 `FOR UPDATE`**，否则会丢更新：快照与删除之间提交的一次 `UPDATE`，
  冷表会拿到更新前的版本，而 `DELETE` 按主键删掉的是更新后的那一行——两个版本都不剩。
  批次上限 2 万行、`lock_timeout` 30 秒，所以这把锁是有界的：拿不到就整批放弃重来，
  而不是让写方无声地输掉。一个单测和一次 scratch 库演练都真的执行过：
  快照开着时并发 `UPDATE` 会撞 `lock_timeout`，不会被悄悄吞掉。
- 搬动 ≥1 行后对热表执行 `VACUUM (ANALYZE)`（**不是** `VACUUM FULL`，后者要 ACCESS EXCLUSIVE 锁）。
  它跑在每表 `try` 的**外面**，自带 30 分钟 `statement_timeout`（`VACUUM_STATEMENT_TIMEOUT_MS`），
  结束后在 `finally` 里把批次超时恢复成配置值（不是恢复成默认值，`--timeout-ms` 因此不会失效）。
  超时记 `vacuum: 'timed_out'`、其它失败记 `'failed'` ＋ `vacuum_error`，
  **都不改变该表的状态**：搬运的正确性不依赖 vacuum。
  第一次搬 19 GB 的 `raw_market_observations` 时 vacuum 超过 10 分钟是很可能的，
  以前那会把一次完全成功的搬运记成 `failed`。
- 绝不搬运比 cutoff 新的行；cutoff 由 `hot_cutoff(now, hot_days)` 计算，`now` 必须带时区。

#### 增量链钳位（为什么搬运要看备份链的水位线）

第 5.1 节的规则允许把一张冷孪生表的数据排除出每夜 dump——前提是那些行**已经**被热表的
分块链导出过。备份侧现在会检查这个前提（水位线够新才排除），但那是**事后**的补救：
链停滞的那几夜，行已经被搬走了。所以搬运侧也有一道闸，两边一起才封得住这个洞。

凡是出现在 `STOCK_BACKUP_INCREMENTAL_TABLES` 里的表，`_move_table` 会先读
`<STOCK_BACKUP_ROOT>\incremental\<表>\state.json`（就是 `Invoke-StockIncrementalBackup` 写的那份），
取出水位线，加进快照谓词：`(<创建列> IS NULL OR <创建列> < 水位线)`，更新列同理。
**没有任何一行会被搬到水位线之上。** 三种结果：

| 情况 | 每表 `status` | 行为 | 运行结果 |
|---|---|---|---|
| 钳位确实扣下了行（链落后于 cutoff） | `chain_behind` ＋ `chain_withheld_rows` | 钳位之前的行照常搬，之后的留在热表 | 告警；**退出码 0**——钳位干了它该干的活，这是工作状态 |
| `state.json` 缺失、读不出、或没有水位线 | `chain_missing` | **这张表一行都不搬** | 告警，`partial`，**退出码 1** |
| `STOCK_BACKUP_INCREMENTAL_TABLES` 写了张表没有的列 | `chain_columns_missing` | **这张表一行都不搬** | 告警，`partial`，**退出码 1** |

后两种进 `BLOCKING_TABLE_STATUSES`，要人来看：一张表的孪生被 dump 排除、而它的分块链读不出来，
这是**备份出了问题**，分层作业每夜对最大的那张表默默什么都不做，正是最贵的那种"绿色"。

`chain_withheld_rows` 是**有界探测**（`CHAIN_PROBE_ROWS = 100000`），超出时记
`chain_withheld_rows_capped: true`。回执需要的是"扣下了一些，大概多少"；
对一张链冻了几个月的表做无界 count 就是在维护窗里做一次全表扫描。
另外 `chain_behind` **不会覆盖 `conflicts`**：被隔离的行是更紧急的发现，
那种情况下钳位由 `chain_withheld_rows` 和 detail 自己说明。

#### 搬运看不懂的唯一索引：拒绝，不猜

`unique_key_columns` 只认普通唯一索引（`indpred IS NULL AND indexprs IS NULL`），
因为冲突扫描没法对"部分索引"和"表达式索引"给出正确的自然键。而
`LIKE ... INCLUDING INDEXES` 会把这类索引原样复制到孪生表上——于是第 4 步的
`ON CONFLICT DO NOTHING` 可能悄悄丢掉一行，还被记成良性的 `already_in_cold_rows`。

所以 `unsupported_unique_indexes(conn, table)` 在**热表和孪生表两侧**都查一遍，
发现任何一个就把这张表记 `unsupported_unique_index`、点名那个索引、**一行都不搬**
（同样进 `BLOCKING_TABLE_STATUSES`，`partial`，退出码 1）；`plan` / `status` 的每表输出里
也带 `unsupported_unique_indexes` 列表。热表侧也查，是因为 `INCLUDING INDEXES` 会让
今天的热表索引变成下次 `install` 之后的孪生表索引——在索引出现的那一夜 06:00 就失败，
好过在 `install` 跑过之后的那一夜才失败。

今天五张分层表的九个唯一索引全是普通索引，所以这是**潜在**而不是现存问题。
`quant-service/tests/test_storage_tier_policy.py` 的
`TieredTablesHaveNoUniqueIndexTheMoveCannotReasonAboutTest` 会扫冻结 DDL 和每一个迁移，
哪天有人给分层表加了个部分／表达式唯一索引，发布门就会红——顺带还有一个测试把识别用的
正则钉在已知的正反样本上，免得有人重写时把守卫变成空操作。

#### 冲突隔离表 `quant.storage_tier_conflicts`

```sql
conflict_id uuid PRIMARY KEY, table_name text, hot_pk jsonb,
hot_row jsonb, cold_row jsonb, detected_at timestamptz
```

- 由 `install` 建在**默认（热）表空间**，刻意**不是**冷孪生表：它可能持有某一行热数据的
  唯一副本，必须进每夜 dump。第 5 节的动态排除规则只丢 `_cold` 结尾的名字，
  所以它天然留在备份里——**任何人都不要把它加进 `STOCK_BACKUP_EXCLUDE_TABLE_DATA`**。
- 隔离后的行**会从热表删掉**，否则每晚都会重试同一批冲突行，永远搬不完。
  两个版本都完整保存在 jsonb 里，没有数据丢失，只是从热表挪到了隔离表。
- 冲突比较用 `=` 而不是 `IS NOT DISTINCT FROM`，与 PostgreSQL 唯一索引语义一致
  （NULL 不与任何值冲突），所以 `raw_market_observations.symbol` 这种可空自然键
  不会因为"两边都是 NULL"被误隔离。
- 出现任何隔离行：本次运行 `status='conflicts'`、`alert=true`、退出码 1。

**运维怎么处理一条被隔离的行**：

```sql
SELECT table_name, hot_pk, detected_at,
       jsonb_pretty(hot_row), jsonb_pretty(cold_row)
FROM quant.storage_tier_conflicts ORDER BY detected_at DESC LIMIT 20;
```

比对两个版本，判断哪一个是对的：冷表里那条保留不动就行；如果热版本才是对的，
手工 `UPDATE quant.T_cold ... WHERE <唯一键>` 覆盖，然后删掉这条隔离记录。
判不出来就两条都留着——隔离表本身就是证据，不要急着清空。

### 2.5 空间策略

热层用量 = `PGDATA_DIR` 的递归磁盘占用（不是 `pg_database_size`，因为 WAL 和临时文件
也吃预算），有三条必须记住的修正：

1. **跳过重解析点（reparse point）。** `pg_tblspc\<oid>` 是指向 `G:\StockPlatform\data\pg-cold`
   的目录联接（junction）。递归统计如果跟着它走进去，整个冷层会被算成热层用量，
   越搬"热层"越大，空间策略永远降不下来。`is_reparse_point()` 在**进入或计数任何条目之前**
   判断（junction / symlink / `FILE_ATTRIBUTE_REPARSE_POINT`，读不出属性的条目按重解析点处理），
   跳过的路径原样写进回执的 `usage.excluded_reparse_points`。
2. **`pg_wal` 单独报，但算在盘里。** `usage_bytes` 含 WAL（它确实占着这块盘），
   `wal_bytes` 单独给出，空间策略用的是 `tiering_usage_bytes = usage_bytes - wal_bytes`：
   一次回补造成的 WAL 尖峰不该被当成"分层能解决的永久增长"。
3. **预算受限于盘。** 有效预算 = `min(配置预算, 当前用量 + 卷剩余空间)`（纯函数
   `effective_budget()`）。配置的 500 GB 装不下时 `fits_volume=false`，本次运行
   `alert=true`，原因写进判定文本。之后所有比例都按**有效预算**算。

阈值（`plan_space_moves`，纯函数，已单测）：

| 用量 / 有效预算 | 动作 | 状态 |
|---|---|---|
| ≤ 85 % | 不动 | `ok` |
| > 85 % | 反复把"最大的分层表"的最老一天交给冷层，直到回落到 75 % 以下 | `reduce` |
| > 95 % | 同上，并把本次运行标记为 `alert` | `reduce` ＋ `alert` |
| 单表本轮已经交出 `--max-space-days` 天（默认 7） | 本轮到此为止，下一夜继续 | `capped` |
| 降不下来且所有分层表都只剩 30 天热数据 | 不再缩窗，只告警 | `exhausted` ＋ `alert` |

`reduce` 和 `capped` 是**工作状态**，不会让整次运行变成 `degraded`；
只有 `unknown` / `exhausted` / `needs_repack` 会（见第 7 节退出码表）。

#### 为什么"搬完了盘还是没变小"是正常的，以及棘轮怎么停

普通 `VACUUM` **不会把页还给文件系统**。删掉最老的行，释放的是堆文件**前部**的页，
PostgreSQL 会愉快地把新插入写回这些页，但截断不了文件尾部，所以文件长度不变。
这意味着：

- **滚动窗口确实能封住增长**——文件不再变大，热层不会无限膨胀；
- **但测出来的目录大小不会下降**，"一直搬到用量降下来"的朴素循环会在一夜之间
  把整年热窗全部吃掉。

所以棘轮是**有界**的：每张表每次运行最多交出 `--max-space-days` 天（默认 7），
**每搬完一张表就重新测量一次目录**；只要真的搬了行、而用量没有下降至少 1 %
（`MIN_USAGE_DROP_RATIO`），棘轮立刻停下，状态 `needs_repack`、`alert=true`、退出码 2。

**那 1 % 量的是 `tiering_usage_bytes`，不是 `usage_bytes`**——也就是空间策略自己判定用的
那个不含 WAL 的量。搬几百万行 `DELETE` ＋ `INSERT` ＋ `VACUUM` 会写出几 GB 的 WAL，
`max_wal_size` 是 4 GB，所以拿含 WAL 的总量去算，降幅很容易是**负**的，
棘轮会因为一个完全不相干的理由停在 `needs_repack`。棘轮每一步的回执里记四个数：
`tiering_usage_before_bytes` / `tiering_usage_after_bytes`（判定用的）和
`wal_before_bytes` / `wal_after_bytes`（解释"为什么总量没动"的），
`needs_repack` 的原因文本把这四个数都点出来。

> **这个作业永远不会自己跑 `VACUUM FULL` 或 `pg_repack`。**
> 两者都要长时间的 ACCESS EXCLUSIVE 锁或额外等量磁盘，属于运维在自己的维护窗里
> 明确决定的动作。看到 `needs_repack` 的正确反应是：先确认滚动窗口已经把增长封住了
> （对比连续几天的 `usage_bytes`），再决定是加盘、还是安排一次带停机预算的
> `VACUUM FULL` / `pg_repack`。不要靠继续缩热窗来"挤空间"，那只是在销毁历史。

估算方式：假设一张表的字节在它的热跨度上均匀分布，用"每天字节数"排步骤；
真实释放量无法预知，所以脚本每一轮都重新测量目录，估算只用来定顺序。

### 2.6 cutoff 索引

搬运语句每批都要 `WHERE <时间列> < cutoff ORDER BY <时间列> LIMIT n`。
没有索引时这是一次 19 GB 的顺序扫描 ＋ 排序，每批都来一遍。所以五张分层表各有一个：

```text
索引名（均在 quant schema 下）                   列
raw_market_observations_tier_cutoff_idx         available_at
tushare_raw_records_tier_cutoff_idx             available_at
intraday_quote_observations_tier_cutoff_idx     observed_at
intraday_rule_input_snapshots_tier_cutoff_idx   observed_at
edge_evidence_changes_tier_cutoff_idx           changed_at
```

实测依据：`quant.raw_market_observations`（19 GB / 770 万行）没有以 `available_at`
打头的索引，每批 20 000 行都规划成 `Limit -> Sort -> Index Scan using
raw_market_availability_basis_idx`，也就是每批扫一遍全表。建索引后每批是一次有界范围扫描。

两条路径都会建出同样五个名字，谁先跑另一个就是 no-op（都带 `IF NOT EXISTS`）：

- `database-storage-tiers.py install`：在 autocommit 连接上 `CREATE INDEX CONCURRENTLY`，
  `lock_timeout` 30 秒、`statement_timeout` 2 小时（19 GB 要两遍全表）。
  这一步排在 `install` 的**最后**，而且是**逐目标容错**的：某个索引拿不到锁就记
  `skipped_locked`，不影响孪生表、视图和表空间搬迁。
- Alembic 迁移 `20260919_0106_storage_tier_cutoff_indexes`（`down_revision = 20260918_0105`）：
  在 `op.get_context().autocommit_block()` 里用 `postgresql_concurrently=True` ＋
  `if_not_exists=True` 建同样五个，这样**从迁移链重建出来的库**一开始就有它们。
  一个测试把迁移里的索引集合钉死在 `TIER_POLICY` 上，两边不可能漂移。

#### INVALID 索引：`install` 会真的重建它

`CREATE INDEX CONCURRENTLY` 被打断（`lock_timeout`、`statement_timeout`、手工取消）会留下一个
`indisvalid = false` 的索引，**名字已经被它占住了**。这是第一次在 19 GB 表上建索引时
最现实的失败形态，而 `IF NOT EXISTS` 只看名字不看有效性——它会发一条 notice 然后什么都不做，
`install` 从前会照样把结果记成 `created`，规划器却继续不用这个索引，每批退回全表扫描。
迁移侧同理：`if_not_exists=True` 也修不了它。

现在 `install` 不问"有没有这个名字"，问的是 `cutoff_index_state`（直接查 `pg_index` 的
`indisvalid`）：

| 发现 | `install` 做什么 | 回执 `result` |
|---|---|---|
| 名字存在且有效 | 什么都不做 | `already_present` |
| 名字不存在，但有别的有效索引以时间列打头 | 什么都不做（沿用旧行为） | `already_present` |
| 名字存在但 **INVALID** | `DROP INDEX CONCURRENTLY` 后重建 | `rebuilt_invalid` |
| INVALID，且 `DROP` 拿不到 ShareUpdateExclusive 锁 | 放弃，等下次 | `invalid_index_present` |
| 新建之后**读回来**发现还是 INVALID（第二遍扫描后才失败的情况） | 不假设成功，如实报告 | `invalid_index_present` |

出现任何 `invalid_index_present` 时 `install` 整体是 **`partial`**（退出码 1），
不会被报成 `ok`；名字汇总在回执的 `invalid_indexes` 里。
迁移的 docstring 现在写明了**修复是 `install` 的职责**，以及迁移为什么对这个状态刻意保持 no-op
（`autocommit_block` 里一次 DROP＋重建无法回滚，而迁移必须是可重放的）。

`plan` 和 `status` 的每表输出里有 `cutoff_index_present` **和 `cutoff_index_valid`**：
前者回答"有没有"，后者回答"能不能用"。只看前者会把一个 INVALID 索引读成健康的。

---

## 3. PostgreSQL 配置

配置文件 `G:\StockPlatform\config\postgresql-stock-platform.conf` 是**生成物**，
由 `postgres-managed-config.psm1` 的 `Get-StockPlatformManagedSettings` 产生，
被集群自己的 `postgresql.conf` 用 `include_if_exists` 引入。
**生产环境不得手工编辑它**——初始化脚本和数据目录迁移脚本都会重新生成，手改会被覆盖。

分层相关的关键值：

```
work_mem = '64MB'                       # 16MB 时排序/回补查询 3.5 天溢出 251 GB 临时文件
temp_file_limit = '20GB'                # 单会话上限，是热层预算的兜底
random_page_cost = 1.1                  # 热层是 NVMe；stock_cold 表空间自带 random_page_cost = 4
checkpoint_completion_target = 0.9
shared_preload_libraries = 'pg_stat_statements'
track_io_timing = on
log_lock_waits = on
log_temp_files = 10240                  # kB：溢出超过 10 MB 就记一行，因为临时文件吃热层预算
```

以下两项仍须保持不变，不能按 Linux 调优经验直接修改：

- `shared_buffers = '4GB'`：Windows 在远未耗尽内存时就拒绝大共享段
  （"could not reserve shared memory region"，错误 487，4GB 下已经约 83 次/天）。调大会更频繁地启动失败。
- `effective_io_concurrency = 0`：Windows 版 PostgreSQL 没有 `posix_fadvise()`，
  唯一合法值就是 0。改成 Linux 上的数值会导致服务器拒绝启动。

`max_connections = 100` 是 2026-09-23 容量复核后的**运行预算**，不是 PostgreSQL 或
Windows 的硬上限。此前的 50 是人工配置值，没有对应的容量压测依据；同机隔离实例
（PostgreSQL 16.15、`shared_buffers=4GB`）在 224 路缓存命中只读客户端下零失败，
但吞吐约在 64 路后进入平台区。100 给 owner 的 20 槽异步池、协作者连接池与后台作业
留出共享余量；高成本查询仍须单独按延迟、临时文件和协作者影响验收。
这项设置只能随受控数据库重启及回退预案变更，不得把隔离读压测结果当成生产重查询容量。

角色设置（由 `database-storage-tiers.py install` 幂等施加，**2026-09-19 已在生产集群生效**，
`pg_db_role_setting` 里就这两条 `stock_peer` 值，没有别的）：

```sql
ALTER ROLE stock_peer SET statement_timeout = '15min';
ALTER ROLE stock_peer SET idle_in_transaction_session_timeout = '5min';
```

`quant_app` / `stock_admin` 不动：业主 API 自己按语句设超时。
注意 `ALTER ROLE ... SET` 是**集群级**的，不能只在某个库里生效——这是它必须写进本文的原因，
也是 `install` 带 `--skip-role-settings` 的原因：拿一个一次性 scratch 库演练 `install` 时
必须加这个开关，否则演练会把 `ALTER ROLE` 打到整个生产集群上。
**生产的 `install` 绝不加这个开关**（由 `test-postgres-storage-tier-wiring.ps1` 断言
runner 里不出现它）。

连接超时（每条连接都带，`connect()` 里写死，不可关）：

- `lock_timeout = 30s`：任何 DDL / 搬运语句等锁超过 30 秒就放弃，不会把集群拖住。
- `statement_timeout`：`plan` / `status` / 搬运批次 10 分钟（`--timeout-ms` 可调）；
  `install` 1 小时（要重写 2.6 GB 的 `legacy_source_records`），
  其中建 `CONCURRENTLY` 索引那一段临时提到 2 小时。

---

## 4. 定时任务与时间窗

### 4.1 维护窗 04:00–08:00

所有重型数据库批处理都排在这个窗口内，顺序固定：

| 时间 | 任务名 | 脚本 | 说明 |
|---|---|---|---|
| 04:10 | `trading-hareness-stock-backup` | `scripts/windows/backup-stock-database.ps1` | 每夜 `pg_dump` ＋ 增量分块导出 ＋ 保留期裁剪 |
| 05:10 | `trading-hareness-stock-backup-offsite` | `scripts/windows/run-stock-backup-offsite.ps1 -Command nightly` | 加密上传百度网盘，并裁剪已验证的本地旧副本 |
| 06:00 | `trading-hareness-storage-tiers` | `scripts/windows/run-storage-tiers.ps1 -Command apply` | 冷热分层搬运 ＋ 预算守卫 |

顺序不是随意的：**分层作业排在两个备份之后**，这样每一夜的 dump 里，那些即将被搬走的行
还在热表上被完整捕获过一次（关于这一点的真实局限，见第 5 节的风险提示）。

runner 还会从 runtime.env 读 `STORAGE_TIER_HOT_DAYS`（默认 365），把它作为 `--hot-days`
传给每一条子命令——同一个键，`backup-stock-database.ps1` 也拿它判断冷孪生的新鲜度，
两侧因此不可能各走各的（见第 1 节的配置键表与第 5 节的"有链不等于链是新的"）。它只从文件里挑这一个键，
runtime.env 里的凭据不会进到 runner 进程或命令行；命令行上显式传的 `--hot-days` 仍然优先。

`run-storage-tiers.ps1` 自带窗口守卫：`Get-StorageTierRunWindowDecision` 在
周一至周五 09:00–15:30 直接跳过（记一行 `skipped: skip_trading_session` 后 exit 0），
除非传 `-Force`。这条守卫**只在启动时检查一次**，所以真正保证作业不会闯进开盘的是
下面这三道停止条件，缺一不可：

| 机制 | 值 | 谁来停 | 停下来的样子 |
|---|---|---|---|
| `--deadline 08:00` | 本地墙钟 HH:MM，runner 默认传给 `apply` | **作业自己** | 每批之间、每张表之间、棘轮每一步之间检查；手上这一批跑完，写回执 `status='deadline_reached'`，**退出码 0** |
| `--max-seconds 7200` | 相对上限，runner 默认传给 `apply` | 作业自己 | 同上，两者**谁先到算谁**（Windows 补跑一个错过的 06:00 触发、或跨午夜时，墙钟可能已经没有意义） |
| `ExecutionTimeLimit 2h15m` | 任务计划程序 | 操作系统 | **兜底，不该被用到**：06:00 起跑 ＋ 08:00 截止 ＝ 2 小时，留 15 分钟余量。被它杀掉的进程**不写任何回执** |
| **没有失败重启** | 任务里刻意不设 `-RestartCount` / `-RestartInterval` | —— | runner 原样透传 CLI 退出码，任务计划程序看到的"上次运行结果"就是回执状态本身；而这个作业每跑一次最多可以把某张表的热窗口砍掉 `DEFAULT_MAX_SPACE_DAYS = 7` 天，重启两次就是 21 天，而每份回执单看都"合规"。`-StartWhenAvailable` 保留：真正错过的 06:00 触发仍然要补跑一次 |

`deadline_reached` 退出码是 0 而不是错误码，因为它是**正常结果**：搬运幂等可续跑，
今晚停在哪儿明晚从哪儿继续。第一次大迁移本来就会连着好几夜才搬完。
（`--deadline` / `--max-seconds` 只加给 `apply`——`plan` / `status` / `install` 不搬行；
显式传的值优先，`-Command apply --deadline 07:00` 仍然有效。）

**但"窗口已经关了"和"干到窗口关"不是一回事。** `-StartWhenAvailable` 会补跑一个错过的触发：
机器周五夜里关着，周六 10:00 开机补跑，窗口守卫放行（周末不是交易时段），
`resolve_deadline` 算出来的 08:00 **已经过去了**。`command_apply` 因此在碰第一张表之前
就检查一次截止时间，已经过期就记 `deadline_missed`（原因里同时点出"现在几点"和"截止几点"），
状态 `deadline_missed`、告警、**退出码 1**。
以前这种情况和"一路干到 08:00"一样报 `deadline_reached` 退出 0，
回执上完全看不出这一夜其实一行都没搬。

I/O 窗任务会在 08:00 后把 PostgreSQL 降到 `BelowNormal`，但那是降级不是停止，
不能替代上面的截止时间。**第一次真实运行仍然请人工盯一次。**

### 4.2 I/O 优先级窗口

`trading-hareness-postgres-io-window` 每 15 分钟跑一次 `set-postgres-io-window.ps1`
（每日 00:02 触发 ＋ 无限重复），执行时限 5 分钟。
这台机器同时是业主的桌面机，所以窗口外的数据库后台工作必须让路：

| 模式 | 时段（本机本地时间 = Asia/Shanghai） | `PriorityClass` | I/O 优先级 |
|---|---|---|---|
| `normal` | 周一至周五 09:00–15:40（盘中＋收盘交接） | `Normal` | 2 |
| `normal` | 每日 16:30–23:00（盘后管线与业主复盘） | `Normal` | 2 |
| `normal` | 每日 04:00–08:00（备份／异地／分层维护窗） | `Normal` | 2 |
| `background` | 其余时间 | `BelowNormal` | 1 |

区间都是左闭右开。I/O 优先级通过 `NtSetInformationProcess(ProcessIoPriority = 33)` 设置，
这是未公开但稳定的接口，失败只记录不致命——优先级类本身已经解决了大部分卡顿感。
脚本幂等，只有模式真的变化（或有进程被改动、有失败）时才往
`G:\StockPlatform\logs\postgres-io-window.jsonl` 写一行。

### 4.3 任务安装

两个新任务的安装器与既有安装器同构（`New-HiddenPowerShellTaskAction` ＋ `run-hidden.vbs`，
当前用户、`RunLevel Limited`、隐藏无控制台）：

```powershell
pwsh -NoProfile -File G:\StockPlatform\current\scripts\windows\install-storage-tiers-task.ps1 `
    -RepositoryRoot G:\StockPlatform\current -HostRoot G:\StockPlatform\current
pwsh -NoProfile -File G:\StockPlatform\current\scripts\windows\install-postgres-io-window-task.ps1 `
    -RepositoryRoot G:\StockPlatform\current -HostRoot G:\StockPlatform\current
```

**这两个安装器不由 `publish-stock-release.ps1` 自动执行**，需要人工注册一次。
这是刻意与既有惯例保持一致：`trading-hareness-stock-backup` 和 `-offsite` 同样是手工注册的，
让发布流水线的影响面保持不变。

两个安装器的 `-RepositoryRoot` / `-HostRoot` **默认值就是 `G:\StockPlatform\current`**，
不是"运行它的那个 checkout"：从 F: 的 worktree 注册出来的任务会在 worktree 被删掉的那一刻
失效，而且只在 06:00 才暴露出来（"系统找不到指定的文件"）。它们也依赖已发布 release 才有的
`scripts\windows\bin\stock-background-host.exe`，所以必须在发布之后、从 `current` 运行。
注册完两个安装器都会把**实际存进任务里的** `Execute` / `WorkingDirectory` / `Arguments`
打印出来——注册错了当场就能看见：

```text
TaskName : trading-hareness-storage-tiers
State    : Ready
Execute  : G:\StockPlatform\current\scripts\windows\bin\stock-background-host.exe
```

想故意注册一份开发副本，显式传 `-RepositoryRoot`。

### 4.4 日志

| 文件 | 内容 |
|---|---|
| `G:\StockPlatform\logs\storage-tiers.jsonl` | `apply` 每次运行一条 JSON：起止时间、每表搬运行数、前后用量、空间策略判定、`status`、`alert`。可用 `PGDATA_TIERS_LOG_FILE` 或 `--log-file` 改写 |
| `G:\StockPlatform\logs\storage-tiers\<yyyy-MM-dd>.log` | 任务当天的纯文本输出（runner 写的，含 Python 的 stdout/stderr）。runner 在调 Python 之前设 `PYTHONIOENCODING=utf-8`：任务宿主控制台是 UTF-8，而重定向到管道时 Python 默认按 `locale.getpreferredencoding()`（本机 cp936）编码，非 ASCII 的失败诊断正好会变成乱码 |
| `G:\StockPlatform\logs\postgres-io-window.jsonl` | I/O 窗模式变更记录 |
| `G:\StockPlatform\logs\stock-backup.jsonl` | 每夜备份回执（既有） |
| `G:\StockPlatform\logs\postgres-data-migration-<时间戳>.json` | 数据目录迁移回执（一次性） |

---

## 5. 备份链

三条链，互不替代：

1. **每夜全量 dump** — `backup-stock-database.ps1`，`pg_dump -Fc`，落在
   `G:\StockPlatform\backups\<yyyy-MM-dd>\trading_hareness-<日期>.dump`，
   附 `.sha256` 与 `.excluded-table-data.json`。保留 14 个自然日 ＋ 之外每 ISO 周 1 份共 8 周。
2. **增量分块** — `STOCK_BACKUP_INCREMENTAL_TABLES`（默认
   `quant.raw_market_observations:created_at:updated_at`）指定的大表按水位线增量导出到
   `G:\StockPlatform\backups\incremental\<表>\`，其数据**在本次增量导出成功之后**才从 dump 中排除；
   增量失败会自动退化为全量 dump 并把本次运行记为 `degraded_full_dump`。
3. **异地副本** — `scripts/stock-backup-offsite.mjs nightly` 加密上传到百度网盘。
   **已核对代码：`listLocalBackupFiles` 同时枚举 `<backups>\<日期>\` 和 `<backups>\incremental\<表>\`，
   增量分块确实会被上传**（`kind: 'incremental'`），`fetch` 也会把完整增量链拉回本地重建备份树。
   因此异地副本不是"只有全量 dump"。

### 5.1 哪些表的数据可以不进 dump（在 dump 时算出来，不是配出来的）

排除的是**数据**不是结构：恢复时这些表会以空表重建，再由增量分块链回填。
冷层最终会长到几十上百 GB，每夜重复 dump 一遍没有恢复价值，只会让备份和上传时间成倍增长——
**但前提是那些行确实被别的东西带着。** 唯一的"别的东西"就是热表的增量分块链：
行还在热表上的时候就被按水位线导出成分块，而分块**从不裁剪**。

所以排除列表由 `backup-stock-database.ps1` 的 `Resolve-StockBackupExcludedTableData`
在**每夜 dump 的时候**按本次运行的 `STOCK_BACKUP_INCREMENTAL_TABLES` 算出来：

| 条目 | 何时排除 |
|---|---|
| 增量表自身的数据（如 `quant.raw_market_observations`） | **仅当本次增量导出成功**。失败则退化为全量 dump（`degraded_full_dump`），不留空洞 |
| 增量表的冷孪生（`quant.<表>_cold`） | **仅当对应热表的分块链既存在、又没有落后于分层截止线**。不取决于今晚这一次导出成功与否（它的行是被**之前几次**的分块链捕获的），但取决于那几次确实追到了截止线 |
| 运维在 `STOCK_BACKUP_EXCLUDE_TABLE_DATA` 里额外列的表 | 照办——**除非**它是 `_cold` 结尾而对应热表不在增量链里、或那条链已经停滞，那种条目被**拒绝**、告警，并记进本次运行记录的 `refused_table_data_exclusions` |

**"有链"不等于"链是新的"。** 分层作业把超过热窗口的行搬进冷孪生表，而分块链只带走它
**已经导出到**的那一段。增量导出一旦开始每夜失败（坏分块、备份盘满、行数对不上），
`<备份根>\incremental\<热表>\state.json` 里的水位线就冻住，而分层作业照搬不误——
被搬走的行于是既不在热表、也不在分块链里，dump 又把冷孪生排除掉，那些行就只剩
G: 冷表空间上的一份活数据。所以规则会读这份 `state.json`：水位线缺失、读不出，
或早于 `now - 热窗口`，冷孪生一律**退回 dump**，并记进本次运行记录的
`refused_table_data_exclusion_reasons`（带上具体原因）。
热窗口取 runtime.env 的 `STORAGE_TIER_HOT_DAYS`，默认 365，与分层策略是同一个数——
字面意义上的同一个：`run-storage-tiers.ps1` 也读这个键，并把 `--hot-days` 传给分层 CLI，
所以调窄热窗口**只需改这个键**，搬运侧和备份侧一起跟着走。只在命令行上临时传
`--hot-days` 而不动这个键（那条路径仍然保留，命令行优先），备份侧就还按 365 天判"够新"。

被拒绝时夜间 dump **照常跑完**：一条配置意见不该让每夜备份停摆。

> **这道闸只是两道之一。** 备份侧判新鲜度是**事后**补救：链停滞的那几夜，行已经被搬走了，
> 备份侧能做的只是从下一夜起把孪生表重新放回 dump。所以搬运侧有配套的一道闸——
> `database-storage-tiers.py` 读同一份 `state.json`，**绝不把任何一行搬到水位线之上**，
> 读不出来就一行都不搬（`chain_missing`，退出码 1）。见 2.4 的"增量链钳位"。
> 两边读的是同一个 `STOCK_BACKUP_INCREMENTAL_TABLES`，两边的默认值由
> `test-postgres-storage-tier-wiring.ps1` 钉死成同一个字符串。

结果是：`STOCK_BACKUP_EXCLUDE_TABLE_DATA` 现在**没有默认值**，
`initialize-stock-platform.ps1` **不再补写**任何冷孪生表（脚本里写明了原因），
它只是一个运维覆盖口子。当前默认增量链只有 `quant.raw_market_observations`，
所以每夜实际排除的就是它自己 ＋ `quant.raw_market_observations_cold` 两项；
另外四张冷孪生表的数据**仍然每夜进 dump**。

规则本身由 `scripts/windows/tests/test-postgres-storage-tier-wiring.ps1`（把规则喂进
`TIER_POLICY` 的五张表 ＋ 新鲜水位线，必须正好得到五张冷孪生表；只喂默认链时必须拒绝另外
四张；水位线冻在截止线之前时必须一张都不排除）和 `test-backup-stock-database.ps1`
（链有/无、水位线新/旧/缺失/损坏、热窗口调窄、导出失败、运维覆盖被拒，外加
`Get-StockIncrementalChainWatermark` 对真实 `state.json` 的读取）双向钉死。

> `quant.storage_tier_conflicts` **不是** `_cold` 结尾，规则天然不会排除它。
> 它可能持有某行热数据的唯一副本，**任何人都不要手工把它加进排除列表**。

### 5.2 原先的两条风险，现在的状态

**风险 A（冷层行走出备份保留期）—— 已解决，靠的是把规则改成动态的。**
原来的问题是：初始化脚本静态地把五张冷孪生表全排除掉，而五张里只有
`quant.raw_market_observations` 有增量链。另外四张的行被搬进冷孪生表之后就不再进任何 dump，
而"搬走前最后一次捕获它的 dump"只保留 14 天 ＋ 8 周——大约两个月后，
它们的唯一副本就是 G: 上冷表空间里的在线数据。
现在排除是按链算的：**没有增量链的冷孪生表永远不会被排除**，它的数据每夜照常进 dump。
生产 `runtime.env` 里从来没有写过那个静态默认值，所以没有产生过实际的备份空洞。

要让另外四张表的冷孪生也享受"不进 dump"的省时省空间，**唯一正确的做法是先把这四张热表
加进 `STOCK_BACKUP_INCREMENTAL_TABLES`**（格式 `<表>:<创建列>:<更新列>`），
让它们有自己的分块链；排除会自动跟上，不需要再改任何排除配置。

**风险 B（恢复脚本在排除列表下会失败）—— 已解决。**
恢复脚本读 dump 旁的 `.excluded-table-data.json`，对其中每一张被排除的表去
`<BackupRoot>\incremental\<表>\` 重放分块。原来目录不存在时
`Import-StockIncrementalChunks` 的 `Get-ChildItem -LiteralPath` 会抛 `ItemNotFoundException`，
而且是在 `CREATE DATABASE` ＋ `pg_restore` 都做完之后才抛——等于每一份 dump 都恢复不了。
现在 `Import-StockIncrementalChunks` 在**碰数据库之前**就返回
`status = 'no_chunk_chain'`（chunks = 0），`restore-stock-database.ps1` 把这些表名
汇总进返回值的 `skipped_no_chunks`，恢复正常完成。
修在模块里而不是恢复脚本里，是为了让**所有调用方**都拿到同样的行为；
两个返回形状现在都带 `status` 字段。
由 `scripts/windows/tests/test-stock-incremental-backup-drill.ps1` 用**真实 PostgreSQL 运行时**
的一次性库演练过：夜间 dump 正好排除了演练表和它的冷孪生，恢复跳过了没有分块目录的那张，
没有失败。

---

## 6. 数据目录迁移与回滚（一次性）

脚本：`scripts/windows/migrate-postgres-data-directory.ps1`

```powershell
# 先干跑，看计划和数字，什么都不改
pwsh -NoProfile -File F:\AIWorkflow\trading_hareness\scripts\windows\migrate-postgres-data-directory.ps1 `
    -TargetDataDir F:\StockPlatformDB\postgresql16 -WhatIf

# 真跑（只能在 04:00-08:00 维护窗内；周一至周五 09:00-15:30 会被拒绝，除非 -Force）
pwsh -NoProfile -File F:\AIWorkflow\trading_hareness\scripts\windows\migrate-postgres-data-directory.ps1 `
    -TargetDataDir F:\StockPlatformDB\postgresql16
```

八个步骤，顺序本身就是安全保证：

1. **预检**（只读）：pwsh 7；`PGDATA_DIR` 不等于目标；不在交易时段（除非 `-Force`）；
   当前目录确实是集群（有 `PG_VERSION`）；`pg_ctl status` 证明服务器正在从该目录运行；
   目标目录不存在或为空；目标盘剩余空间 ≥ 源目录大小 ＋ 50 GB（`-FreeSpaceMarginGB`）；
   `runtime.env` 可写；`robocopy` 存在；报告源目录里**枚举不到**的条目数（不再静默丢弃，
   否则第 4 步的文件／字节对比会莫名其妙地失败）。
   **行数快照不在这一步抓**：此刻业主 API、看板运行时、共享对端隧道和 04:10／05:10／06:00
   三个维护窗作业都还在写库，在这里抓的数会被它们合法写入的行推翻，让一次本来成功的迁移
   中止。快照挪到第 3 步。
2. **先停看门狗任务**，顺序是 **`Disable-ScheduledTask` → 优雅停 → `Stop-ScheduledTask` 兜底**，
   这个顺序由 `test-postgres-data-migration-contract.ps1` 断言：
   1. `Disable-ScheduledTask`：只阻止**新**实例启动，不杀任何正在跑的东西；
   2. `stop-stock-dashboard.ps1`：优雅停 adapter、隧道与业主 API。它自己写运行时停止标记、
      自己杀监听 PID，所以 supervisor 观察到的是一次**预期内**的退出，状态记成 `stopped`；
   3. `Stop-ScheduledTask`：兜底，收拾优雅停没够着的。

   **把 3 放到 2 前面（脚本原来的写法）会一次性终结任务的整个 job object**
   ——watchdog、supervisor、被监管进程同时死，`Request-RuntimeStop` 根本没机会跑，
   运行时状态文件会留下一个"healthy"指着一个已经死掉的 PID。
   `publish-stock-release.ps1` 的 `Stop-ProductionRuntime` 写的是同一条规则。

   停的是 `trading-hareness-dashboard-runtime`（它会自己把 PostgreSQL 拉起来，
   不先停就会在半拷贝的目录上再起一个服务器）、`trading-hareness-post-close-pipeline`、
   `trading-hareness-storage-tiers`、`trading-hareness-stock-backup`、
   `trading-hareness-stock-backup-offsite`——后两个正好排在同一个维护窗里，
   不停它们就会有 `pg_dump` 打到一个停机或半拷贝的集群上。
   脚本只记录**它自己禁用过**的任务，第 8 步也只重新启用这些，绝不会打开运维故意关掉的任务。

   **`trading-hareness-shared-peer-tunnels` 故意不停**，理由写在脚本里也由测试断言：
   停 PostgreSQL 本身就已经切断了所有 peer 会话，隧道留着只是一条没人用的空管道；
   多停一个任务就多一个"忘了再打开"的失败面，而且第 8 步的恢复路径也就多一个分支。
3. **在停机窗口内**抓 `quant.instruments`、`quant.canonical_bars_daily`、
   `quant.raw_market_observations` 的行数快照——此刻平台运行时已经停了、集群还在，
   下一句就把它停掉，所以这份数正是第 6 步该对上的数；对不上就只可能是真差异。
   然后 `pg_ctl stop -m fast -w`，并确认没有 `postgres.exe` 残留（否则直接失败）。
4. `robocopy <旧> <新> /E /COPY:DAT /DCOPY:DAT /XJ /R:2 /W:2 /MT:8 /NP /LOG+:<日志>`，
   随后**三重校验**：robocopy 退出码 < 8、源与副本的递归文件数与字节数完全相等、
   `global\pg_control` 的 SHA-256 相等。校验没过就绝不进入下一步。

   `/XJ` 是必须的：`pg_tblspc\<oid>` 是指向 G: 冷表空间的目录联接，
   跟着它走 robocopy 会把整个冷层复制进 NVMe。脚本因此**先自己数重解析点**
   （`Get-DirectoryFootprint` 显式跳过，不依赖 PowerShell 7 的默认行为），
   把 `pg_tblspc` 下的联接在目标端**按原 target 重建并校验**；
   源目录里出现 `pg_tblspc` **之外**的任何重解析点，本次迁移**直接拒绝**运行
   ——与其被 `/XJ` 无声丢掉，不如停下来让人看一眼。
5. 把 `PGDATA_DIR` 写进 `runtime.env`，重新生成 `postgresql-stock-platform.conf`
   （与初始化脚本共用 `Write-StockPlatformManagedConfig`，两边输出逐字节相同）。
6. 从新目录启动，`pg_isready`，然后校验：`SHOW data_directory` 等于目标路径、
   `SHOW work_mem` = `64MB`、`shared_preload_libraries` 含 `pg_stat_statements`、
   三张表行数与第 1 步快照一致。
7. 把旧目录**改名**为 `postgresql16.pre-nvme-<yyyyMMdd>`（`-KeepOldName` 可改），
   **永不删除**——它是唯一的迁移前完整镜像，由运维日后手工清理；写出 JSON 回执。
8. `Start-ScheduledTask trading-hareness-dashboard-runtime`，轮询业主 API `http://127.0.0.1:5681/health`
   最多 5 分钟，重新启用第 2 步禁用过的任务。

### 失败了会怎样（第 2–8 步任意一步）

以前没有 `catch`：任何一步抛异常都会把 PostgreSQL 停着、平台下线、五个任务禁用着直接退出。
现在 2–8 步整体包在 `try/catch` 里，失败时 `Invoke-FailureRecovery` 逐步把平台放回去，
每一步都是尽力而为、逐条记录，**最后重新抛出原始异常**——恢复本身失败也绝不掩盖停下来的原因：

- **还没给旧目录改名**（第 7 步之前）：旧目录仍是权威副本。停掉可能从目标目录起来的服务器、
  把 `PGDATA_DIR` 写回旧目录、重新生成托管配置、从旧目录启动 PostgreSQL，**最后**把本次运行
  在目标目录留下的半成品拷贝删掉——顺序不能反：源集群确认起来之前不能动另一份数据。
  不删的话，重试时会被预检的"目标目录存在且非空"挡住，脚本等于被自己的残渣卡死。
  这个删除有三道互相独立的闸（只删本次运行建的、旧目录尚未改名、路径不等于源目录），
  并且先逐个解除 `pg_tblspc` 联接再递归删，免得跟着联接把 G: 上的冷表空间一起删掉。
- **已经改名**（第 7 步之后）：新目录已经被启动并校验过，数据在 NVMe 上，**不能抛弃**。
  这时只恢复平台，**绝不把 `PGDATA_DIR` 指回一个已经不装着集群的目录**。
- 两种情况都会再跑一次 `Start-PlatformRuntimes`（重新启用**本次运行禁用过的**任务并拉起平台），
  并写一份 `.failure.json` 回执，里面是每一步恢复动作的结果。

### 回滚

```powershell
# 先看年龄差，什么都不动
pwsh -NoProfile -File ...\migrate-postgres-data-directory.ps1 `
    -Rollback -RollbackDataDir G:\StockPlatform\data\postgresql16.pre-nvme-20260919 -WhatIf

# 真回滚：必须显式接受数据丢失
pwsh -NoProfile -File ...\migrate-postgres-data-directory.ps1 `
    -Rollback -RollbackDataDir G:\StockPlatform\data\postgresql16.pre-nvme-20260919 -AcceptDataLoss
```

**回滚会丢掉切换之后写入的每一行。** 那个目录是迁移当晚的镜像，不是副本、不会跟进。
所以有三道闸：

1. **先打印年龄差**：两边的 `pg_controldata` "Time of latest checkpoint"，以及相差多少小时／天。
   日期格式与区域设置有关，解析不出来时回退到 `global\pg_control` 的 mtime，
   绝不因为格式问题而"放行"；目标目录根本不是集群时报 `unavailable`。
2. **`-AcceptDataLoss` 是硬性要求**：不加就拒绝执行（`-WhatIf` 下打印
   `refused = accept_data_loss_required` 并返回，真跑时在停任何东西**之前**就抛出）。
3. **冷表空间闸，绝对拒绝，没有开关可绕**，两种情形：
   - 活集群已经有 `stock_cold` 表空间、而镜像早于它的创建时间（镜像的 `pg_tblspc` 是空的）：
     回滚会让每一行搬进冷层的数据凭空消失；
   - 两边的 `pg_tblspc` 都有联接、且**指向同一个目录**，而活集群的 checkpoint 比镜像新：
     冷表空间既不在任何一个数据目录里，也从来没被迁移复制或版本化过，
     用旧目录起来就是拿一份旧目录录去描述已经被新集群改写过的冷层文件——
     那不是回退，是一个自相矛盾的集群。

   这两条 `-Force` 和 `-AcceptDataLoss` 都绕不过去。为此迁移回执里记了
   `tablespaces`（`pg_tablespace` 的 oid、名字、location），回滚闸就是拿它和两边
   `pg_tblspc` 里的联接目标比对的。

   **另外：回滚从来不回退 `stock_cold` 本身。** 不管闸放不放行，每一次
   `-Rollback` 都会打印这句话，回执里也记 `stock_cold_reverted: false`。

过了闸之后：停任务与 PostgreSQL，把 `PGDATA_DIR` 指回旧目录，重新生成配置，从旧目录启动，
再把平台拉起来，并写自己的回执。NVMe 上的副本保留在原地供检查，不会被删除。
回滚的前提是旧目录还在——所以第 7 步只改名不删除。
迁移回执里打印的回滚命令**故意不带 `-AcceptDataLoss`**：照抄执行会被拒绝，
这正是想要的效果。切换几小时之后，正确做法是从备份恢复，不是回滚。

> 目前生产的 `pg_tblspc` 还是空的（`install` 尚未运行），所以第 3 道闸只有静态契约测试覆盖，
> 没有真实触发过。**`install` 跑完之后，请再跑一次 `-Rollback -WhatIf` 看它真的拒绝。**

### 迁移之后必须补做的一次性动作

1. 注册两个新任务（见 4.3），注册前先手工跑一次
   `run-storage-tiers.ps1 -Command status` 确认发布后的目录布局正确。
2. 运行 `database-storage-tiers.py install`（见下一节）。它必须在**迁移重启之后**运行，
   因为 `pg_stat_statements` 要等 `shared_preload_libraries` 生效后才能 `CREATE EXTENSION`；
   而且它会以 `ACCESS EXCLUSIVE` 锁重写 2.6 GB 的 `quant.legacy_source_records`，只能在维护窗内做。

---

## 7. 命令与核对

分层 CLI（Python，用发布版虚拟环境运行）：

```powershell
$py = 'G:\StockPlatform\current\.venv\Scripts\python.exe'
$tiers = 'G:\StockPlatform\current\scripts\database-storage-tiers.py'
$envf = 'G:\StockPlatform\config\runtime.env'

& $py $tiers status  --env-file $envf     # 用量/预算、每表冷热行数与最老热行（只读事务）
& $py $tiers plan    --env-file $envf     # apply 会搬什么：每表待搬行数、按天分布、空间判定（只读事务）
& $py $tiers install --env-file $envf     # 幂等：建表空间、孪生表、视图、cutoff 索引、隔离表、角色超时、整表冷放置
& $py $tiers apply   --env-file $envf     # 真搬，并写 storage-tiers.jsonl
```

公共参数：`--env-file`、`--hot-days N`、`--budget-bytes 500GB`、`--batch N`、
`--table T`（可重复，接受 `quant.x` 或 `x`）、`--pgdata-dir`、`--cold-dir`、`--tablespace`、
`--timeout-ms`、`--max-batches`、`--max-space-days N`（默认 7）、`--deadline HH:MM`（默认无）、
`--max-seconds N`、`--log-file`、`--backup-root`（读 `incremental\<表>\state.json` 的根目录，
默认 `STOCK_BACKUP_ROOT` 或 `G:\StockPlatform\backups`；**演练时务必指到 scratch 目录**，
免得钳位读到生产备份根）。
子命令专有：`plan --day-limit N`（默认 30）、`install --skip-role-settings`
（**只在 scratch 库演练时用**，见第 3 节）。
四个子命令都只输出**一行 ASCII JSON**（任务宿主控制台是 GBK，所以回执强制 ASCII 转义）。
`plan` / `status` 用 `default_transaction_read_only=on` 连接，由服务器保证它们写不了生产库。

### 退出码

退出码是给任务计划程序和运维看的，**不要把 2 当成"泛泛的失败，重试就好"**：

| 码 | `status` | 含义 | 该做什么 |
|---|---|---|---|
| **0** | `ok` | 正常完成 | 无 |
| **0** | `deadline_reached` | 到点自己停的，搬到哪算哪 | 无。明晚继续 |
| **1** | `deadline_missed` | 起跑时截止时间**已经过去**，一张表都没碰 | 看为什么补跑到了窗口之外（见 4.1）。不是"搬完了" |
| **1** | `partial` | 某张表失败了或被拒绝了，其余表正常 | 看一眼；下面那张表说明是哪一类 |
| **1** | `conflicts` | 有行被隔离进 `quant.storage_tier_conflicts` | 按 2.4 末尾处理隔离行 |
| **1** | `schema_drift` | 某张冷孪生表与热表不可自动调和 | **下次运行之前**必须人工处理（见 2.3） |
| **2** | `degraded` | **500 GB 守卫没在守**：用量测不出来（`unknown`）、热窗已到 30 天下限（`exhausted`）、棘轮停在 `needs_repack`，**或 `install` 根本没跑过**（只在 `plan` / `status` 上） | 真要处理的一档：加盘、或安排一次 `VACUUM FULL`/`pg_repack` 维护窗、或先把 `install` 跑了 |
| **2** | `failed` | 命令本身抛异常（回执里只有 `error`） | 看日志 |

`status` 的优先级是
`degraded > schema_drift > partial > conflicts > deadline_missed > deadline_reached > ok`。
`schema_drift` 压过 `partial`，因为别的每表失败明晚重试一次就没了，而漂移的孪生表不会自己好。
`run-storage-tiers.ps1` **原样透传** Python 的退出码，不做任何翻译
（由 `test-postgres-storage-tier-wiring.ps1` 逐条钉死状态→码的映射，
并把这份清单反向钉在 CLI 的 `EXIT_CODES` 上：新加一个状态而忘了在测试里定码，发布门会红）。

#### 哪些每表状态会把整次运行拉成 `partial`

`apply` 的 `status` 不只看 `errors`。以下每表状态**无条件**让运行至少是 `partial`（退出码 1）：

| 每表 `status` | 含义 | 归属 |
|---|---|---|
| `skipped_missing_table` | 孪生表不存在 | `NOT_INSTALLED_TABLE_STATUSES` |
| `skipped_missing_quarantine` | `quant.storage_tier_conflicts` 不存在 | `NOT_INSTALLED_TABLE_STATUSES` |
| `chain_missing` | 分块链的 `state.json` 读不出来（见 2.4） | `BLOCKING_TABLE_STATUSES` |
| `chain_columns_missing` | 增量配置写了张表没有的列 | `BLOCKING_TABLE_STATUSES` |
| `unsupported_unique_index` | 部分／表达式唯一索引，搬运看不懂（见 2.4） | `BLOCKING_TABLE_STATUSES` |

前两个的意思都是"`install` 没跑过"——**而那正是今天生产的状态**。
从前它们不在任何一条阶梯上，于是一次什么都没搬的运行报 `ok` 退出 0，
任务计划程序上一片绿，而 500 GB 守卫其实一夜也没守过。

同一件事在只读侧：`plan` 和 `status` 会先查表空间、隔离表、以及每张存在的热表是否有孪生表
（`_read_only_status`）。缺任何一样就答 **`degraded`（退出码 2）**，
并在 `not_installed` / `reason` 里点名缺的是什么。
孪生表单独查，是因为 `install` 是**逐表容错**的：装了一半是真会发生的状态，
后果和完全没装一样——`apply` 对那张表什么都搬不了。

> 所以**今天**在生产上跑：`status` / `plan` 答 `degraded` 退出 2，`apply` 答 `partial` 退出 1。
> 这是正确的，不是回归——它说的是"`install` 还没跑"。跑完 `install` 之后才该看到 `ok`。

PowerShell 入口（任务用的就是它；清代理、写日志、带窗口守卫）：

```powershell
pwsh -NoProfile -File G:\StockPlatform\current\scripts\windows\run-storage-tiers.ps1 -Command status
pwsh -NoProfile -File G:\StockPlatform\current\scripts\windows\run-storage-tiers.ps1 -Command apply -Force
# 多余参数原样转发给 Python CLI：
pwsh -NoProfile -File ...\run-storage-tiers.ps1 -Command plan --table quant.raw_market_observations
```

I/O 窗口：

```powershell
pwsh -NoProfile -File ...\scripts\windows\set-postgres-io-window.ps1 -WhatIf      # 只看判定与计划
pwsh -NoProfile -File ...\scripts\windows\set-postgres-io-window.ps1 -Mode normal # 运维临时强制
```

SQL 侧核对：

```sql
-- 表空间在哪、有多大
SELECT spcname, pg_tablespace_location(oid) FROM pg_tablespace WHERE spcname = 'stock_cold';
SELECT pg_size_pretty(pg_tablespace_size('stock_cold'));

-- 每张表和它的索引落在哪个表空间（空 = pg_default = 热层）
SELECT c.relname, coalesce(t.spcname, 'pg_default') AS tablespace,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS size
FROM pg_class c
LEFT JOIN pg_tablespace t ON t.oid = c.reltablespace
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'quant' AND (c.relname LIKE '%\_cold' OR c.relname = 'legacy_source_records')
ORDER BY 1;

-- 冷热是否真的不重叠（应当返回 0）
SELECT count(*) FROM quant.raw_market_observations_cold
WHERE available_at >= now() - interval '365 days';

-- 五个 cutoff 索引都建上了吗（应当返回 5 行，且 indisvalid 全为 t；
-- CONCURRENTLY 中途失败会留下 indisvalid = f 的索引，重跑 install 会 DROP CONCURRENTLY
-- 后重建并把结果记成 rebuilt_invalid；plan/status 的 cutoff_index_valid 也看得到）
SELECT c.relname, i.indisvalid
FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'quant' AND c.relname LIKE '%\_tier\_cutoff\_idx' ORDER BY 1;

-- 有没有被隔离的冲突行（正常是 0 行）
SELECT table_name, count(*), max(detected_at)
FROM quant.storage_tier_conflicts GROUP BY 1;
```

守卫测试：

```powershell
# PowerShell（发布前由 publish-stock-release.ps1 自动执行）
pwsh -NoProfile -File scripts\windows\tests\test-postgres-storage-tier-wiring.ps1
pwsh -NoProfile -File scripts\windows\tests\test-postgres-io-window.ps1
pwsh -NoProfile -File scripts\windows\tests\test-postgres-data-migration-contract.ps1
pwsh -NoProfile -File scripts\windows\tests\test-backup-stock-database.ps1
# 这个会真的起 PostgreSQL、建一次性库并删掉，跑得比上面几个慢
pwsh -NoProfile -File scripts\windows\tests\test-stock-incremental-backup-drill.ps1

# Python
G:\StockPlatform\current\.venv\Scripts\python.exe -m pytest `
    tests/test_storage_tier_policy.py tests/test_database_storage_tiers.py `
    tests/test_migration_contracts.py -q   # cwd = quant-service
```

上面这些**都不碰数据库**，所以发布门跑得起。真正执行一次搬运的测试是
`ScratchDatabaseMoveTest`（在 `test_database_storage_tiers.py` 里），它**默认跳过**，
需要一个可连的集群和 `CREATE DATABASE` 权限，发布门两样都没有。手工跑：

```powershell
$env:STORAGE_TIERS_SCRATCH_TEST = '1'          # 不设就跳过
# 另外需要 PG* 连接变量（从 runtime.env 载进进程环境，值绝不打印）
# 库名默认 trading_hareness_tiers_unittest，由测试自己建、自己删
G:\StockPlatform\current\.venv\Scripts\python.exe -m pytest `
    tests/test_database_storage_tiers.py -k ScratchDatabaseMove -q   # cwd = quant-service
```

它真的执行 `_move_table`：行数守恒、自然键冲突被隔离、增量链钳位正好扣下未导出的那些行、
`state.json` 缺失时一行都不搬、以及一把真实的 `FOR UPDATE` 锁挡住并发 `UPDATE`。
**这类测试是必要的**：它第一次跑就抓到了一个 `NameError`（`unique_keys` 未定义），
而那条路径上所有"语句形状"断言全都视而不见地通过了——
字符串断言只能证明 SQL 长什么样，证明不了它跑得起来。

---

## 8. 怎么新增一张分层表

1. 在 `scripts/database-storage-tiers.py` 的 `TIER_POLICY` 里加一条
   `TierPolicy("quant", "<表>", "<timestamptz 列>", <热窗天数>)`。
2. 在本文 2.1 的表格（`tier-policy-table` 标记之间）加同一行，顺序与代码一致。
3. 在 `quant-service/migrations/versions/20260919_0106_storage_tier_cutoff_indexes.py` 的
   `INDEXES` 里加一条 `<表>_tier_cutoff_idx`——或者写一个新的迁移建它。
   一个测试把这份清单钉死在 `TIER_POLICY` 上，漏了会红。
4. **不要动任何备份排除配置。** 排除是在 dump 时按增量链算的（第 5.1 节）：
   新表的冷孪生会自动留在 dump 里，直到有人把这张热表加进
   `STOCK_BACKUP_INCREMENTAL_TABLES`——那才是解锁排除的正确动作。
5. 跑 `test_storage_tier_policy.py`、`test_database_storage_tiers.py`、
   `test_migration_contracts.py` 和 `test-postgres-storage-tier-wiring.ps1`
   ——分别校验策略与 schema、纯函数、迁移链、以及 PowerShell 与 Python 两侧的一致性。
6. 在下一个维护窗里跑一次 `install`（建孪生表、视图和 cutoff 索引），
   再跑 `plan` 确认搬运量和 `cutoff_index_present` 符合预期。

前置条件：这张表必须有一个 `timestamptz` 列作为时间基准，必须是只增不改的证据表
（被更新的行搬到冷层后，更新会打在空的热表上），并且**不能有任何应用代码要查它的完整历史**
——需要完整历史的查询只能走 `quant.<表>_all` 视图，而那是运维路径。

---

## 9. 决策记录

| 编号 | 决策 | 理由 |
|---|---|---|
| **D1** | 不用 VHDX、不用 NTFS 配额来限制 500 GB，改为由分层作业执行"软上限" ＋ `temp_file_limit` 兜底 | 两者都需要管理员权限，本次会话拿不到 UAC。升级路径已记录：拿到管理员会话后可转成固定 500 GB 的 VHDX |
| **D2** | G: 保留完整图景：在线冷层 ＋ 每夜逻辑备份链 ＋ 改名保留的迁移前数据目录 `postgresql16.pre-nvme-<日期>`；不做 `pg_basebackup` | `pg_basebackup` 每夜会往机械盘上复制最多 500 GB，成本远超收益；逻辑备份 ＋ 增量链已经覆盖恢复需求 |
| **D3** | 不用 PostgreSQL 原生分区，改用冷孪生表 ＋ `UNION ALL` 视图 | 分区要求分区键进入主键，而这些表的主键是 uuid、且写路径依赖 `ON CONFLICT` 唯一约束；改主键会撞上 `test_migration_contracts.py` 的冻结 DDL 哈希，等于一次全库重写 |
| **D4** | `shared_buffers` 保持 4GB | Windows 的 "could not reserve shared memory region"（错误 487）在 4GB 下已经约 83 次/天，调大只会让启动更容易失败 |
| **D5** | 冷孪生表用 `LIKE ... INCLUDING DEFAULTS INCLUDING INDEXES`，刻意不带外键与触发器 | 冷行是历史证据，不应再参与引用完整性；删掉一个 `instruments` 行不该级联删除一年前的观测。代价是应用代码必须永不引用冷层，由守卫测试强制 |
| **D6** | `effective_io_concurrency` 永远是 0 | Windows 版 PostgreSQL 无 `posix_fadvise()`，非 0 值会让服务器拒绝启动。这是"别看着像没调优就去调"的头号陷阱 |
| **D7** | 运行记录写 JSONL（`logs\storage-tiers.jsonl`），不接 `runtime-observability.psm1` 的 `Write-RuntimeEvent` | 与 `stock-backup.jsonl` 同构，运维已经习惯这种查法；分层作业是 Python 进程，走 PowerShell 模块要多一层桥接，增加失败面 |
| **D8** | 维护窗定为 04:00–08:00，窗内顺序固定为 04:10 dump → 05:10 异地 → 06:00 分层 | 分层必须排在备份之后，否则当夜的 dump 会缺掉刚被搬走的行；三者又都必须避开盘中，否则和交易时段抢同一块盘 |
| **D9** | I/O 让路用"进程优先级类 ＋ `NtSetInformationProcess` I/O 优先级"，不做限速器 | Windows 没有 per-database I/O 调度器；改优先级是幂等、可回退、零依赖的做法，I/O 优先级设置失败也只降级不致命 |
| **D10** | 热层用量**排除重解析点**、**单独报 `pg_wal`**；分层决策用 `usage_bytes - wal_bytes` | `pg_tblspc` 下的联接指向 G: 冷层，跟着走会把冷层算成热层，越搬越大。WAL 确实占着这块盘（所以算进 `usage_bytes`），但搬证据行不可能让它变小，一次回补的 WAL 尖峰不该被读成分层能解决的永久增长 |
| **D11** | 有效预算 = `min(配置预算, 当前用量 + 卷剩余)`，装不下就告警 | 配置里写 500 GB 而盘上只剩 80 GB 时，按 500 GB 算比例永远是"健康"，守卫等于没有 |
| **D12** | 棘轮每表每次最多 `--max-space-days`（默认 7）天，每表之后重新测量；用量不降 1 % 就停在 `needs_repack`，**永不自动跑 `VACUUM FULL` / `pg_repack`** | 普通 `VACUUM` 不把页还给文件系统，所以"一直搬到用量下降"会在一夜之间吃光整年热窗。滚动窗口足以封住增长（新插入复用释放的页），回收既有膨胀是带停机预算的运维决定 |
| **D13** | 搬运改成**先插后按主键删**（TEMP 快照 ＋ 显式列名），不再用 `ctid` 的单语句 CTE | 单语句 CTE 里的 `ON CONFLICT DO NOTHING` 会**无声销毁**一条自然键相同但内容不同的热行。拆成两步才能在删之前检测冲突；而 `ctid` 跨语句不稳定（`VACUUM`、HOT 更新都会挪行），所以必须用主键 |
| **D14** | 自然键冲突整行隔离进 `quant.storage_tier_conflicts`（**默认表空间**），并把该行从热表删掉 | 隔离表可能持有某行的唯一副本，必须进每夜 dump，所以不能放冷层、不能叫 `_cold`。删掉热行是为了不让同一批冲突行每晚重试到天荒地老；两个版本都完整留在 jsonb 里 |
| **D15** | 冲突判定用 `=` 而不是 `IS NOT DISTINCT FROM` | 与 PostgreSQL 唯一索引语义一致：NULL 不与任何值冲突。否则 `raw_market_observations.symbol` 这种可空自然键会因为"两边都是 NULL"被误隔离一批本来根本不会碰撞的行 |
| **D16** | 所有 SQL 用**显式列名**，视图、快照、插入一律不用 `SELECT *` | `CREATE VIEW ... SELECT *` 会在建视图时把 `*` 展开冻结；之后热表加一列，视图会永远少那一列且不报错。等于把五张表的 schema 冻在 `install` 那一天 |
| **D17** | 作业**自己**带截止时间（`--deadline` / `--max-seconds`），`ExecutionTimeLimit` 只作兜底 | 被任务计划程序杀掉的进程**不写任何回执**，第二天早上没有任何证据说明它搬到哪儿了。自己停下来才能写 `deadline_reached` 并以 0 退出 |
| **D18** | 备份排除在 **dump 时按增量链算**，初始化脚本不再补写任何默认值 | 静态排除一张没有增量链的冷孪生表，等于两个月后（14 天 ＋ 8 周保留期）把它的唯一副本变成 G: 上的在线数据。规则必须跟着 `STOCK_BACKUP_INCREMENTAL_TABLES` 走，不能是一份会过期的配置 |
| **D19** | 数据目录迁移**不停** `trading-hareness-shared-peer-tunnels` | 停 PostgreSQL 本身已经切断了所有 peer 会话，隧道只是空管道；少停一个任务就少一个"忘了再打开"的失败面和一条恢复分支 |
| **D20** | `-Rollback` 要 `-AcceptDataLoss`，并在冷表空间比镜像新时**绝对拒绝**（无开关可绕） | 普通的"镜像有点旧"可以由人知情后接受；但镜像早于 `stock_cold` 创建时，每一行搬进冷层的数据会凭空消失，这不是重启能挽回的，只能从备份恢复 |
| **D21** | 冷孪生表的排除还要看增量链**水位线是否追得上分层截止线**，不只看链在不在；热窗口从 runtime.env 的 `STORAGE_TIER_HOT_DAYS` 读（默认 365） | 分层作业只看行的年龄，不看链导出到哪儿。链一旦停滞（坏分块、盘满、行数对不上），被搬走的行既不在热表、也不在链里，而 dump 还照排除不误——没有任何症状，直到需要恢复 |
| **D22** | 分层任务**不注册失败重启** | runner 原样透传 CLI 退出码，任务计划程序分不清"暂时性失败"和"需要人来看"；而每跑一次最多砍 7 天热窗口，重启两次就是 21 天，且每份回执单看都合规。要重试就在 runner 里按状态分流，不能交给调度器 |
| **D23** | 迁移的行数快照挪进停机窗口（第 3 步），预检只报告"枚举不到的条目" | 预检那一刻业主 API、看板运行时、隧道和三个维护窗作业都还在写库，在那里抓的数会被合法写入推翻，让一次成功的迁移中止在最后一步 |
| **D24** | 失败恢复会删掉本次运行留在目标目录的半成品；回滚闸改成比对**表空间 location** 而不是联接数量 | 不删，重试会被自己的残渣挡在预检外；只数联接则看不见"两个镜像指向同一个 `pg-cold`"——那种回滚会拿旧目录录去描述已被改写的冷层文件 |
| **D25** | 搬运侧也钳位到分块链水位线（`chain_behind` / `chain_missing` / `chain_columns_missing`），不只在备份侧判新鲜度 | 备份侧的新鲜度检查是事后补救：链停滞的那几夜行已经搬走了。两边一起才封得住"行既不在热表、也不在链里、dump 又排除了孪生"这个洞。`chain_behind` 退出 0（钳位干活了），后两个退出 1（读不出链＝备份出问题，而作业每夜对最大的表默默什么都不做） |
| **D26** | `install` 没跑过时：`plan` / `status` 答 `degraded`（退出 2），`apply` 因 `skipped_missing_*` 至少是 `partial`（退出 1） | 这是**今天生产的真实状态**。从前它报 `ok` 退出 0——一个一行都没搬、守卫一夜没守过的运行，在任务计划程序上是绿的。孪生表单独查，是因为 `install` 逐表容错，装了一半的后果和没装一样 |
| **D27** | `install` 对 INVALID 的 cutoff 索引 `DROP CONCURRENTLY` 后重建（`rebuilt_invalid`），拿不到锁记 `invalid_index_present` 并让 `install` 收在 `partial`；新建后**读回**有效性而不是假设成功 | `IF NOT EXISTS` 只看名字：一个被打断的 `CONCURRENTLY` 留下的 INVALID 索引会永远占着名字，`install` 报 `created`，规划器却继续全表扫描。迁移刻意不修（`autocommit_block` 里 DROP＋重建不可回滚，而迁移必须可重放），职责写进 docstring |
| **D28** | 快照带 `FOR UPDATE`；`VACUUM` 移出每表 `try`，自带 30 分钟上限，失败只记不改状态 | 没有行锁就会丢更新：快照与删除之间的一次 `UPDATE`，冷表存旧版本、`DELETE` 删新版本，两个都不剩。反过来 `VACUUM` 超时不该把一次**已经成功**的搬运记成 `failed`——19 GB 表第一次搬完，vacuum 超过 10 分钟很正常 |
| **D29** | 部分／表达式唯一索引 → 拒绝搬这张表（`unsupported_unique_index`），热表和孪生表两侧都查 | 冲突扫描对这类索引给不出正确的自然键，而 `LIKE ... INCLUDING INDEXES` 会把它复制到孪生表上，于是 `ON CONFLICT DO NOTHING` 可能悄悄丢一行、还被记成良性的 `already_in_cold_rows`。查热表侧，是为了在索引出现的那一夜就失败，而不是等下次 `install` 把它复制过去之后 |
| **D30** | 棘轮的 1 % 量 `tiering_usage_bytes`（不含 WAL），回执同时记 WAL 前后值 | 搬几百万行写出几 GB WAL，`max_wal_size` 4 GB，含 WAL 的降幅很容易是负的——棘轮会因为一个和分层完全无关的理由停在 `needs_repack`。判定要用和策略同一个量 |

---

## 10. 协作方（peer）与后续 agent 必须知道的

- **数据目录不再是 `G:\StockPlatform\data\postgresql16`。** 生产热目录是
  `F:\StockPlatformDB\postgresql16`，权威来源是 `runtime.env` 的 `PGDATA_DIR`。
  任何文档、脚本、runbook 里写死旧路径都是错的。运行时可以从
  `scripts/stock-platform-context.py` 的 `database_storage` 字段读到当前布局。
- **`stock_peer` 角色带上了超时，2026-09-19 已在生产集群生效**：`statement_timeout = 15min`、
  `idle_in_transaction_session_timeout = 5min`。一个忘了关的 psql 会话不再能跨整个维护窗持锁；
  长查询会被打断，这是刻意的，需要更久请显式 `SET LOCAL`。
- **别在热表上跑全历史扫描。** 要一年以上的数据请查 `quant.<表>_all` 视图，
  并且只在窗口外查（冷层在机械盘上）。
- **新的大证据表必须登记分层策略**，否则它会独占热层预算，直到空间策略开始削别人的历史。
  登记步骤见第 8 节。
- **应用代码永不引用 `*_cold` / `*_all`。** 这是 `AGENTS.md` 里的规则，
  由 `quant-service/tests/test_storage_tier_policy.py` 强制。
- **批处理作业属于 04:00–08:00 窗口。** 要加一个碰数据库的定时任务，先排进这个窗口，
  并确认它不会和 04:10 / 05:10 / 06:00 三个已有任务重叠；如果它可能在迁移期间触发，
  还要把任务名加进 `migrate-postgres-data-directory.ps1` 的 `$watcherTasks`。
- **生产配置文件是生成物。** 想改 PostgreSQL 参数，改
  `scripts/windows/postgres-managed-config.psm1` 的 `Get-StockPlatformManagedSettings`，
  然后重跑初始化或迁移脚本；手改 `postgresql-stock-platform.conf` 会被下一次生成覆盖。
- **退出码 2 不是"重试一下就好"。** 它表示 500 GB 守卫本身失效了（用量测不出来、
  热窗到底、棘轮停在 `needs_repack`，或者 `install` 压根没跑过）。
  给分层作业加自动重试等于每晚重复同一个失败——任务也因此**刻意不注册失败重启**（D22）。
- **分层作业依赖增量备份链。** 它读 `<STOCK_BACKUP_ROOT>\incremental\<表>\state.json`
  把搬运钳制在水位线之前。所以增量导出连着失败不只是备份的问题：
  从第二夜起分层作业就会报 `chain_behind`，链彻底读不出时报 `chain_missing` 并**停止搬运那张表**。
  修增量导出比调分层策略优先。
- **不要手工往 `STOCK_BACKUP_EXCLUDE_TABLE_DATA` 里加冷孪生表。**
  排除是每夜按增量链算出来的；没有链的孪生表会被拒绝并告警。想让某张表的孪生不进 dump，
  正确动作是把那张**热表**加进 `STOCK_BACKUP_INCREMENTAL_TABLES`。
  `quant.storage_tier_conflicts` 永远不能进排除列表。

### 当前实际状态（2026-09-19）

- 生产 `trading_hareness` 里**还没有**任何 `*_cold` 表、`*_tier_cutoff_idx` 索引、
  `quant.storage_tier_conflicts` 表，也没有 `stock_cold` 表空间——`install` 尚未运行。
  第一次 `install` 会直接按上面描述的形态建出来，不需要迁就任何历史形状。
- **因此现在跑 `status` / `plan` 会答 `degraded` 退出 2，跑 `apply` 会答 `partial` 退出 1**
  （见第 7 节）。这是设计出来的答案，不是故障：它说的就是"`install` 还没跑"。
  `install` 跑完之后才该看到 `ok`。
- 生产 `G:\StockPlatform\config\runtime.env` 里目前**没有** `PGDATA_DIR`、
  `PGDATA_BUDGET_BYTES`、`PGDATA_COLD_TABLESPACE_DIR`、`STOCK_BACKUP_INCREMENTAL_TABLES`、
  `STOCK_BACKUP_EXCLUDE_TABLE_DATA` 中的任何一个，所以活机上什么都还没被改动，
  也从来没有写进过那份有问题的静态排除列表。
- `status` 显示最老的热行约 **156 天**，而热窗是 365 天；用量约 **33 GB**，预算 500 GB。
  也就是说**第一次 `apply` 会搬 0 行，而且未来七个月左右都是 0 行**。
  **"第一次跑搬了 0 行"是正确结果，不是故障。**
- 两个新任务都**还没注册**（`New-HiddenPowerShellTaskAction` 依赖已发布 release 里的
  `scripts\windows\bin\stock-background-host.exe`）。带这些脚本的 release 发布之后，
  按 4.3 用 `G:\StockPlatform\current` 下的安装器注册一次。
