# 股票平台数据库增量备份

## 为什么

`quant.raw_market_observations` 每天新增数百万行（2026-09-16 已有 9.3 GB 数据、2 GB 索引），
数据库和备份都在同一块机械盘 G 盘上。每晚全量 `pg_dump` 从 20:30 跑到 21:19，期间
和实时采集抢磁盘，看板服务启动查询超时。数据还会继续增长，全量备份不可持续。

## 怎么做

每晚 `trading-hareness-stock-backup`（`scripts/windows/backup-stock-database.ps1`）：

1. 对 `STOCK_BACKUP_INCREMENTAL_TABLES` 里的表（默认只有 `raw_market_observations`）
   做增量导出：取上次水位线到 `now() - 30 分钟` 之间**新增**（`created_at`）或
   **被修改**（`updated_at`，由触发器自动写入）的行，按上海自然日切成
   `backups\incremental\<表>\<下界>_<上界>.copy.gz`，并写同名 `.json` 清单
   （行数、列、SHA-256）。每个文件导出后都和数据库核对行数，一致后才推进
   `state.json` 水位线；中途失败，下次从断点继续。
2. 增量**全部成功**才对这些表使用 `pg_dump --exclude-table-data`；增量失败时仍做
   全量 dump，记录为 `degraded_full_dump`，任务退出码为 1。
3. 结果追加到 `logs\stock-backup.jsonl`（`excluded_table_data`、`incremental`、
   `incremental_error`），dump 旁写 `*.excluded-table-data.json`。

保留规则：日期目录（基础 dump）仍按 14 天每日 + 8 周每周保留；
`incremental\` 下的分片**永不自动删除**——它们是这张表历史的唯一备份。

说明：
- 30 分钟安全延迟意味着当晚备份不含最近 30 分钟的行，由次日备份补上。
- 线上被删除的行仍保留在分片里，还原出的历史可能比线上多。
- 首次运行会把整张表按天导出一遍（一次性，耗时较长）。

## 还原

只还原到**新库**，脚本拒绝写入 `runtime.env` 里配置的线上库：

```powershell
pwsh -File scripts\windows\restore-stock-database.ps1 `
  -DumpFile G:\StockPlatform\backups\<日期>\trading_hareness-<日期>.dump `
  -TargetDatabase trading_hareness_restore_<日期>
```

步骤：校验 dump 的 SHA-256 → `pg_restore` 基础 dump → 按文件名顺序回放每个分片
（先校验 SHA-256，经临时表按主键 upsert，后面的版本覆盖前面的，行数必须等于清单）。
回放期间外键触发器关闭，输出里列出需要复核的外键约束。核对无误后再决定是否切换平台。

## 验证

- `scripts/windows/tests/test-backup-stock-database.ps1`：纯函数（配置解析、窗口切分、SQL）。
- `scripts/windows/tests/test-stock-incremental-backup-drill.ps1`：在真实 PostgreSQL 上建
  临时库做完整演练（导出、修改后再导出、每晚脚本端到端、同日重跑、还原脚本、
  逐行指纹比对、篡改分片被拒），结束后删除临时库，不读写线上库。
