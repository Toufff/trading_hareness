# 业主数据库存储分层（OWNER_DATABASE_STORAGE）

本文件是**业主 PostgreSQL 数据库物理布局的唯一权威文档**。改动数据目录、备份、
定时维护任务，或改动分层策略里列出的任何一张表之前，先读完本文。

- 适用范围：Windows 工作站上的 `trading_hareness` 集群（PostgreSQL 16.15，端口 `55432`）。
- 不适用：`intraday_edge`（47 机）与 parquet/百度网盘四层协议，那是
  [`DATA_TIERING_PROTOCOL.md`](DATA_TIERING_PROTOCOL.md) 的范围。两份文档互不覆盖：
  本文管"业主库的字节落在哪块盘上"，那份管"哪一类数据属于哪一层"。
- 相关实现：`scripts/database-storage-tiers.py`、`scripts/windows/run-storage-tiers.ps1`、
  `scripts/windows/set-postgres-io-window.ps1`、`scripts/windows/migrate-postgres-data-directory.ps1`、
  `scripts/windows/postgres-managed-config.psm1`。

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
| `STOCK_BACKUP_EXCLUDE_TABLE_DATA` | 五张冷孪生表（分号分隔） | 每夜 dump 跳过其**数据**的表；表结构仍然导出。见第 5 节。 |

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
CREATE OR REPLACE VIEW quant.<表>_all AS
  SELECT * FROM quant.<表> UNION ALL SELECT * FROM quant.<表>_cold;
```

`LIKE` **不会**复制外键与触发器，这正是孪生表想要的：冷行不再参与引用完整性，
删除一个 `instruments` 行不应该级联删掉一年前的观测证据。
代价是冷层没有外键保护，因此有一条硬规则：

> **应用代码永远不得引用 `*_cold` / `*_all` 对象。** 它们是运维与追溯工具。
> 由 `quant-service/tests/test_storage_tier_policy.py` 强制。

### 2.4 搬运机制

每张表循环执行下面这条语句，直到返回 0 行：

```sql
WITH batch AS (
  SELECT ctid FROM quant.T WHERE <列> < %(cutoff)s ORDER BY <列> LIMIT %(batch)s
), moved AS (
  DELETE FROM quant.T WHERE ctid IN (SELECT ctid FROM batch) RETURNING *
), inserted AS (
  INSERT INTO quant.T_cold SELECT * FROM moved ON CONFLICT DO NOTHING RETURNING 1
)
SELECT (SELECT count(*) FROM moved), (SELECT count(*) FROM inserted);
```

- 删除与插入在**同一条语句**里完成，不存在"行既不在热表也不在冷表"的瞬间。
- 一批一个事务，默认 `--batch 20000`；`statement_timeout` 每批 10 分钟，`lock_timeout` 30 秒。
- 幂等且可续跑：中断后重跑只是继续搬剩下的行；`ON CONFLICT DO NOTHING` 命中说明上一次
  跑到一半被打断，回执里记为 `already_in_cold_rows`，不是丢数据。
- 搬动 ≥1 行后对热表执行 `VACUUM (ANALYZE)`（**不是** `VACUUM FULL`，后者要 ACCESS EXCLUSIVE 锁）。
- 绝不搬运比 cutoff 新的行；cutoff 由 `hot_cutoff(now, hot_days)` 计算，`now` 必须带时区。

### 2.5 空间策略

热层用量 = `PGDATA_DIR` 的递归磁盘占用（不是 `pg_database_size`，因为 WAL 和临时文件
也吃预算）。阈值（`plan_space_moves`，纯函数，已单测）：

| 用量 / 预算 | 动作 |
|---|---|
| ≤ 85 % | 不动 |
| > 85 % | 反复把"最大的分层表"的最老一天交给冷层，直到回落到 75 % 以下 |
| > 95 % | 同上，并把本次运行标记为 `alert` |
| 降不下来且所有分层表都只剩 30 天热数据 | 状态 `exhausted` ＋ `alert`：不再缩窗，只告警。此时正确动作是加盘或改策略，不是继续销毁历史 |

估算方式：假设一张表的字节在它的热跨度上均匀分布，用"每天字节数"排步骤；
真实释放量无法预知，所以脚本每一轮都重新测量目录，估算只用来定顺序。

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

角色设置（由 `database-storage-tiers.py install` 幂等施加）：

```sql
ALTER ROLE stock_peer SET statement_timeout = '15min';
ALTER ROLE stock_peer SET idle_in_transaction_session_timeout = '5min';
```

`quant_app` / `stock_admin` 不动：业主 API 自己按语句设超时。
注意 `ALTER ROLE ... SET` 是**集群级**的，不能只在某个库里生效——这是它必须写进本文的原因。

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
除非传 `-Force`。这条守卫**只在启动时检查一次**：任务的执行时限是 4 小时，
所以第一次大搬运理论上可能跑到 10:00 还没结束。这被接受（每批都是独立事务、可中断、可续跑，
而且 I/O 窗任务会在 08:00 后把 PostgreSQL 降到 `BelowNormal`），但**第一次真实运行请人工盯一次**。

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
让发布流水线的影响面保持不变。安装器必须从 `G:\StockPlatform\current` 运行
（它们依赖已发布 release 才有的 `scripts\windows\bin\stock-background-host.exe`）。

### 4.4 日志

| 文件 | 内容 |
|---|---|
| `G:\StockPlatform\logs\storage-tiers.jsonl` | `apply` 每次运行一条 JSON：起止时间、每表搬运行数、前后用量、空间策略判定、`status`、`alert`。可用 `PGDATA_TIERS_LOG_FILE` 或 `--log-file` 改写 |
| `G:\StockPlatform\logs\storage-tiers\<yyyy-MM-dd>.log` | 任务当天的纯文本输出（runner 写的，含 Python 的 stdout/stderr） |
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

### 5.1 冷孪生表为什么被排除

`STOCK_BACKUP_EXCLUDE_TABLE_DATA` 默认由 `initialize-stock-platform.ps1` 补写为五张冷孪生表，
`backup-stock-database.ps1` 把它们追加到 `pg_dump --exclude-table-data`：

- 排除的是**数据**不是结构，恢复时会重建出空表；
- 冷层最终会长到几十上百 GB，每夜重复 dump 一遍没有任何恢复价值，只会让每夜备份和上传时间成倍增长；
- 静态排除与增量导出是否成功**无关**（增量失败时它们仍然被排除），
  因为它们的行早在热表侧就被 dump 捕获过。

排除列表必须与 `TIER_POLICY` 的冷孪生表保持一致，
由 `scripts/windows/tests/test-postgres-storage-tier-wiring.ps1` 强制。

### 5.2 已知风险（未解决，必须知情）

**风险 A：冷层行会走出备份保留期。**
五张分层表里只有 `quant.raw_market_observations` 在增量分块链中。另外四张
（`tushare_raw_records`、`intraday_quote_observations`、`intraday_rule_input_snapshots`、
`edge_evidence_changes`）的行被搬到冷孪生表之后就不再进入任何 dump，而"搬走前最后一次捕获它的 dump"
只按 14 天 ＋ 8 周保留。也就是说 **大约两个月后，这些行的唯一副本就是 G: 上冷表空间里的在线数据**，
没有备份、没有异地副本。这在 G: 磁盘故障时是真实的数据丢失。
可选的解决方向（本次未实施，需要单独评估）：把四张表也纳入增量分块链；
或给冷孪生表做一次性的季度归档 dump；或接受"一年以上的原始证据只保留在线单副本"并明确写进风险登记。

**风险 B：`restore-stock-database.ps1` 在当前排除列表下会失败。**
恢复脚本读取 dump 旁的 `.excluded-table-data.json`，并对其中**每一张**被排除的表去
`<BackupRoot>\incremental\<表>\` 重放增量分块。冷孪生表没有增量目录，
`Import-StockIncrementalChunks` 里的 `Get-ChildItem -LiteralPath $directory` 会抛
`ItemNotFoundException`（已在 PowerShell 7 上实测确认）。
**在修好之前，恢复演练请先把冷孪生表从那个 JSON 里删掉，或对每张冷表建一个空目录再运行恢复。**
正确的修法是让恢复脚本跳过没有增量目录的被排除表；因为它属于恢复路径、需要配合
`test-stock-incremental-backup-drill.ps1` 一起验证，本次没有改动，作为交接遗留项列在这里。

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
2. **先停看门狗任务**：`trading-hareness-dashboard-runtime` 会自己把 PostgreSQL 拉起来，
   不先停它就会在半拷贝的目录上再起一个服务器。同时停掉并禁用
   `trading-hareness-post-close-pipeline`、`trading-hareness-storage-tiers`、
   `trading-hareness-stock-backup`、`trading-hareness-stock-backup-offsite`
   ——后两个正好排在同一个维护窗里，不停它们就会有 `pg_dump` 打到一个停机或半拷贝的集群上。
   脚本只记录**它自己禁用过**的任务，第 8 步也只重新启用这些，绝不会打开运维故意关掉的任务。
   然后调用 `stop-stock-dashboard.ps1` 停掉 adapter、隧道与业主 API。
3. `pg_ctl stop -m fast -w`，并确认没有 `postgres.exe` 残留（否则直接失败）。
4. `robocopy <旧> <新> /E /COPY:DAT /DCOPY:DAT /R:2 /W:2 /MT:8 /NP /LOG+:<日志>`，
   随后**三重校验**：robocopy 退出码 < 8、源与副本的递归文件数与字节数完全相等、
   `global\pg_control` 的 SHA-256 相等。校验没过就绝不进入下一步。
5. 把 `PGDATA_DIR` 写进 `runtime.env`，重新生成 `postgresql-stock-platform.conf`
   （与初始化脚本共用 `Write-StockPlatformManagedConfig`，两边输出逐字节相同）。
6. 从新目录启动，`pg_isready`，然后校验：`SHOW data_directory` 等于目标路径、
   `SHOW work_mem` = `64MB`、`shared_preload_libraries` 含 `pg_stat_statements`、
   三张表行数与第 1 步快照一致。
7. 把旧目录**改名**为 `postgresql16.pre-nvme-<yyyyMMdd>`（`-KeepOldName` 可改），
   **永不删除**——它是唯一的迁移前完整镜像，由运维日后手工清理；写出 JSON 回执。
8. `Start-ScheduledTask trading-hareness-dashboard-runtime`，轮询业主 API `http://127.0.0.1:5681/health`
   最多 5 分钟，重新启用第 2 步禁用过的任务。

### 回滚

```powershell
pwsh -NoProfile -File ...\migrate-postgres-data-directory.ps1 `
    -Rollback -RollbackDataDir G:\StockPlatform\data\postgresql16.pre-nvme-20260919
```

回滚同样先停任务与 PostgreSQL，把 `PGDATA_DIR` 指回旧目录，重新生成配置，从旧目录启动，
再把平台拉起来。NVMe 上的副本保留在原地供检查，不会被删除。
回滚的前提是旧目录还在——所以第 7 步只改名不删除。

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
& $py $tiers install --env-file $envf     # 幂等：建表空间、孪生表、视图、角色超时、整表冷放置
& $py $tiers apply   --env-file $envf     # 真搬，并写 storage-tiers.jsonl
```

公共参数：`--env-file`、`--hot-days N`、`--budget-bytes 500GB`、`--batch N`、
`--table T`（可重复，接受 `quant.x` 或 `x`）、`--pgdata-dir`、`--cold-dir`、`--tablespace`、
`--timeout-ms`、`--max-batches`、`--log-file`；`plan` 另有 `--day-limit N`（默认 30）。
四个子命令都只输出**一行 ASCII JSON**（任务宿主控制台是 GBK，所以回执强制 ASCII 转义）。
退出码：0 成功，1 部分失败（`apply` 的单表失败不影响其他表），2 命令本身抛异常。
`plan` / `status` 用 `default_transaction_read_only=on` 连接，由服务器保证它们写不了生产库。

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
```

守卫测试：

```powershell
# PowerShell（发布前由 publish-stock-release.ps1 自动执行）
pwsh -NoProfile -File scripts\windows\tests\test-postgres-storage-tier-wiring.ps1
pwsh -NoProfile -File scripts\windows\tests\test-postgres-io-window.ps1
pwsh -NoProfile -File scripts\windows\tests\test-postgres-data-migration-contract.ps1
pwsh -NoProfile -File scripts\windows\tests\test-backup-stock-database.ps1

# Python
G:\StockPlatform\current\.venv\Scripts\python.exe -m pytest `
    tests/test_storage_tier_policy.py tests/test_database_storage_tiers.py -q   # cwd = quant-service
```

---

## 8. 怎么新增一张分层表

1. 在 `scripts/database-storage-tiers.py` 的 `TIER_POLICY` 里加一条
   `TierPolicy("quant", "<表>", "<timestamptz 列>", <热窗天数>)`。
2. 在本文 2.1 的表格（`tier-policy-table` 标记之间）加同一行，顺序与代码一致。
3. 把新的冷孪生表名加进 `initialize-stock-platform.ps1` 里
   `STOCK_BACKUP_EXCLUDE_TABLE_DATA` 的默认值，并在生产 `runtime.env` 里同步更新
   （初始化脚本**不会**覆盖已存在的值）。
4. 跑 `test_storage_tier_policy.py`、`test_database_storage_tiers.py` 和
   `test-postgres-storage-tier-wiring.ps1`——三者分别校验策略与 schema、纯函数、
   以及 PowerShell 与 Python 两侧的一致性。
5. 在下一个维护窗里跑一次 `install`（建孪生表和视图），再跑 `plan` 确认搬运量符合预期。

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

---

## 10. 协作方（peer）与后续 agent 必须知道的

- **数据目录不再是 `G:\StockPlatform\data\postgresql16`。** 生产热目录是
  `F:\StockPlatformDB\postgresql16`，权威来源是 `runtime.env` 的 `PGDATA_DIR`。
  任何文档、脚本、runbook 里写死旧路径都是错的。运行时可以从
  `scripts/stock-platform-context.py` 的 `database_storage` 字段读到当前布局。
- **`stock_peer` 角色带上了超时**：`statement_timeout = 15min`、
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
