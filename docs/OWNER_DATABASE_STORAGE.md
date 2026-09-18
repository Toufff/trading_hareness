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

三个键都可选，都有文档化默认值，写在 `G:\StockPlatform\config\runtime.env`：

| 键 | 默认值 | 含义 |
|---|---|---|
| `PGDATA_DIR` | `G:\StockPlatform\data\postgresql16` | 热数据目录；生产为 `F:\StockPlatformDB\postgresql16`。由 `initialize-stock-platform.ps1` 写入，由数据目录迁移脚本改写。 |
| `PGDATA_BUDGET_BYTES` | `536870912000`（500 GB） | 热层预算。仅在缺失时由初始化脚本补写（`Set-StockPlatformEnvDefault`），不会覆盖运维已调过的值。 |
| `PGDATA_COLD_TABLESPACE_DIR` | `G:\StockPlatform\data\pg-cold` | `stock_cold` 表空间目录。同样只补写不覆盖。 |
| `STOCK_BACKUP_EXCLUDE_TABLE_DATA` | **无默认值（初始化脚本刻意不补写）** | 纯运维覆盖项。每夜 dump 真正跳过哪些表的数据是**在 dump 时按增量链算出来的**，不是配置出来的；这个键只用于额外追加，且无增量链的冷孪生表会被拒绝。见第 5 节。 |

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
CREATE TEMPORARY TABLE tier_batch ON COMMIT DROP AS
  SELECT <显式列> FROM quant.T WHERE <列> < %(cutoff)s ORDER BY <列> LIMIT %(batch)s;

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
- 搬动 ≥1 行后对热表执行 `VACUUM (ANALYZE)`（**不是** `VACUUM FULL`，后者要 ACCESS EXCLUSIVE 锁）。
- 绝不搬运比 cutoff 新的行；cutoff 由 `hot_cutoff(now, hot_days)` 计算，`now` 必须带时区。

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
  `skipped_locked`（并可能留下一个 INVALID 索引，重跑 `install` 会重建），
  不影响孪生表、视图和表空间搬迁。
- Alembic 迁移 `20260919_0106_storage_tier_cutoff_indexes`（`down_revision = 20260918_0105`）：
  在 `op.get_context().autocommit_block()` 里用 `postgresql_concurrently=True` ＋
  `if_not_exists=True` 建同样五个，这样**从迁移链重建出来的库**一开始就有它们。
  一个测试把迁移里的索引集合钉死在 `TIER_POLICY` 上，两边不可能漂移。

`plan` 和 `status` 的每表输出里有 `cutoff_index_present`，可以直接看有没有建上。

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

保持不变、且**不要"优化"**的值：

- `shared_buffers = '4GB'`：Windows 在远未耗尽内存时就拒绝大共享段
  （"could not reserve shared memory region"，错误 487，4GB 下已经约 83 次/天）。调大会更频繁地启动失败。
- `effective_io_concurrency = 0`：Windows 版 PostgreSQL 没有 `posix_fadvise()`，
  唯一合法值就是 0。改成 Linux 上的数值会导致服务器拒绝启动。
- `max_connections = 50`。

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

`run-storage-tiers.ps1` 自带窗口守卫：`Get-StorageTierRunWindowDecision` 在
周一至周五 09:00–15:30 直接跳过（记一行 `skipped: skip_trading_session` 后 exit 0），
除非传 `-Force`。这条守卫**只在启动时检查一次**，所以真正保证作业不会闯进开盘的是
下面这三道停止条件，缺一不可：

| 机制 | 值 | 谁来停 | 停下来的样子 |
|---|---|---|---|
| `--deadline 08:00` | 本地墙钟 HH:MM，runner 默认传给 `apply` | **作业自己** | 每批之间、每张表之间、棘轮每一步之间检查；手上这一批跑完，写回执 `status='deadline_reached'`，**退出码 0** |
| `--max-seconds 7200` | 相对上限，runner 默认传给 `apply` | 作业自己 | 同上，两者**谁先到算谁**（Windows 补跑一个错过的 06:00 触发、或跨午夜时，墙钟可能已经没有意义） |
| `ExecutionTimeLimit 2h15m` | 任务计划程序 | 操作系统 | **兜底，不该被用到**：06:00 起跑 ＋ 08:00 截止 ＝ 2 小时，留 15 分钟余量。被它杀掉的进程**不写任何回执** |

`deadline_reached` 退出码是 0 而不是错误码，因为它是**正常结果**：搬运幂等可续跑，
今晚停在哪儿明晚从哪儿继续。第一次大迁移本来就会连着好几夜才搬完。
（`--deadline` / `--max-seconds` 只加给 `apply`——`plan` / `status` / `install` 不搬行；
显式传的值优先，`-Command apply --deadline 07:00` 仍然有效。）

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
| 增量表的冷孪生（`quant.<表>_cold`） | **无条件排除**。它的行是被**之前几次**的分块链捕获的，不取决于今晚这一次 |
| 运维在 `STOCK_BACKUP_EXCLUDE_TABLE_DATA` 里额外列的表 | 照办——**除非**它是 `_cold` 结尾而对应热表不在增量链里，那种条目被**拒绝**、告警，并记进本次运行记录的 `refused_table_data_exclusions` |

被拒绝时夜间 dump **照常跑完**：一条配置意见不该让每夜备份停摆。

结果是：`STOCK_BACKUP_EXCLUDE_TABLE_DATA` 现在**没有默认值**，
`initialize-stock-platform.ps1` **不再补写**任何冷孪生表（脚本里写明了原因），
它只是一个运维覆盖口子。当前默认增量链只有 `quant.raw_market_observations`，
所以每夜实际排除的就是它自己 ＋ `quant.raw_market_observations_cold` 两项；
另外四张冷孪生表的数据**仍然每夜进 dump**。

规则本身由 `scripts/windows/tests/test-postgres-storage-tier-wiring.ps1`（把规则喂进
`TIER_POLICY` 的五张表，必须正好得到五张冷孪生表；只喂默认链时必须拒绝另外四张）
和 `test-backup-stock-database.ps1`（链有/无、导出失败、运维覆盖被拒）双向钉死。

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
   `runtime.env` 可写；`robocopy` 存在；抓取 `quant.instruments`、`quant.canonical_bars_daily`、
   `quant.raw_market_observations` 的行数快照。
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
3. `pg_ctl stop -m fast -w`，并确认没有 `postgres.exe` 残留（否则直接失败）。
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
  把 `PGDATA_DIR` 写回旧目录、重新生成托管配置、从旧目录启动 PostgreSQL。
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
3. **冷表空间闸，绝对拒绝，没有开关可绕**：一旦活集群已经有 `stock_cold` 表空间、
   而镜像早于它的创建时间，回滚会让每一行搬进冷层的数据凭空消失——
   这不是重启能挽回的，只能从备份恢复。这一条 `-Force` 和 `-AcceptDataLoss` 都绕不过去。

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
`--max-seconds N`、`--log-file`。
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
| **1** | `partial` | 某张表失败了，其余表正常（错误在 `errors` 里） | 看一眼；明晚会自动重试，分层本身完好 |
| **1** | `conflicts` | 有行被隔离进 `quant.storage_tier_conflicts` | 按 2.4 末尾处理隔离行 |
| **1** | `schema_drift` | 某张冷孪生表与热表不可自动调和 | **下次运行之前**必须人工处理（见 2.3） |
| **2** | `degraded` | **500 GB 守卫没在守**：用量测不出来（`unknown`）、热窗已到 30 天下限（`exhausted`）、或棘轮停在 `needs_repack` | 真要处理的一档：加盘、或安排一次 `VACUUM FULL`/`pg_repack` 维护窗、或改分层策略 |
| **2** | `failed` | 命令本身抛异常（回执里只有 `error`） | 看日志 |

`status` 的优先级是 `degraded > schema_drift > partial > conflicts > deadline_reached > ok`。
`schema_drift` 压过 `partial`，因为别的每表失败明晚重试一次就没了，而漂移的孪生表不会自己好。
`run-storage-tiers.ps1` **原样透传** Python 的退出码，不做任何翻译
（由 `test-postgres-storage-tier-wiring.ps1` 逐条钉死状态→码的映射）。

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
-- CONCURRENTLY 中途失败会留下 indisvalid = f 的索引，重跑 install 会重建）
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
  热窗到底、或棘轮停在 `needs_repack`）。给分层作业加自动重试等于每晚重复同一个失败。
- **不要手工往 `STOCK_BACKUP_EXCLUDE_TABLE_DATA` 里加冷孪生表。**
  排除是每夜按增量链算出来的；没有链的孪生表会被拒绝并告警。想让某张表的孪生不进 dump，
  正确动作是把那张**热表**加进 `STOCK_BACKUP_INCREMENTAL_TABLES`。
  `quant.storage_tier_conflicts` 永远不能进排除列表。

### 当前实际状态（2026-09-19）

- 生产 `trading_hareness` 里**还没有**任何 `*_cold` 表、`*_tier_cutoff_idx` 索引、
  `quant.storage_tier_conflicts` 表，也没有 `stock_cold` 表空间——`install` 尚未运行。
  第一次 `install` 会直接按上面描述的形态建出来，不需要迁就任何历史形状。
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
