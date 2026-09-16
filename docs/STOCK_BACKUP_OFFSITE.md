# 股票平台数据库异地备份（百度网盘）

本地增量备份见 [STOCK_BACKUP_INCREMENTAL.md](STOCK_BACKUP_INCREMENTAL.md)。本文只讲**把备份
复制到百度网盘**这件事：为什么、怎么运作、怎么还原、出问题看哪里。

## 为什么

- 数据库和备份都在同一块 12 TB 机械盘（G 盘）上，盘坏了两样一起没。
- 数据已到 TB 级预期，**不能每晚全量上传**：单个文件会超过网盘 20 GB 上限，
  带宽也跟不上。所以只上传**每个文件一次**——基础 dump 每天一份，增量分片只传新增的，
  云端照样是一份完整可还原的历史链。
- 百度网盘是第三方存储，**上传前必须加密**。

## 数据流

```text
每晚 20:30  trading-hareness-stock-backup
             └─ G:\StockPlatform\backups\<日期>\*.dump (+ .sha256, .excluded-table-data.json)
                G:\StockPlatform\backups\incremental\<表>\*.copy.gz (+ .json 清单)

每晚 21:30  trading-hareness-stock-backup-offsite
             ├─ 列出本地备份文件，跳过"大小和修改时间都已记账"的
             ├─ AES-256-GCM 加密到 backups\offsite\spool\（临时）
             ├─ 分片上传到 /apps/股票paper存储/db-backups/trading_hareness/files/<相对路径>.enc
             ├─ 用 filemetas 核对云端大小，一致才写 state.json
             ├─ 上传加密后的清单 catalog/offsite-state.json.enc（还原入口）
             └─ 删除"已上传且超过保留期"的本地副本（默认 3 天）
```

分片：单个 `.enc` 超过 `STOCK_BACKUP_OFFSITE_PART_BYTES`（默认 16 GB）会切成
`…enc.part000`、`…enc.part001`，还原时按序拼接。

## 组件

| 文件 | 作用 |
|---|---|
| `feishu-adapter/stock-backup-offsite.mjs` | 加解密、清单与上传计划、本地清理、云端取回（纯逻辑 + 可注入的 pan 客户端） |
| `feishu-adapter/baidu-pan-storage.mjs` | 百度网盘 API：设备码授权、令牌加密存库、获取上传域名、流式分片上传、filemetas、下载 |
| `scripts/stock-backup-offsite.mjs` | 命令行入口 |
| `scripts/windows/run-stock-backup-offsite.ps1` | 读 `runtime.env` → 传进进程环境 → 调 CLI → 结果写日志 |
| `scripts/windows/install-stock-backup-offsite-task.ps1` | 注册计划任务 |

## 配置（`G:\StockPlatform\config\runtime.env`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BAIDU_PAN_APP_KEY` / `BAIDU_PAN_SECRET_KEY` | 无 | 开放平台应用凭据。SecretKey 同时是 OAuth 令牌的加密密钥，**换了它已存的令牌就解不开**，要重新授权。 |
| `STOCK_BACKUP_OFFSITE_REMOTE_ROOT` | `/apps/股票paper存储/db-backups/trading_hareness` | 云端根目录。应用只能读写 `/apps/{应用名}`。 |
| `STOCK_BACKUP_OFFSITE_KEY_FILE` | `G:\StockPlatform\config\backup-offsite.key` | 备份加密密钥（32 字节 base64）。 |
| `STOCK_BACKUP_LOCAL_RETENTION_DAYS` | `3` | 云端已核验的本地副本保留几天。 |
| `STOCK_BACKUP_OFFSITE_PART_BYTES` | `16 GB` | 单个云端文件的大小上限，超出拆 `.partNNN`。 |
| `STOCK_BACKUP_OFFSITE_SLICE_BYTES` | `32 MB` | 网盘分片上传的分片大小（SVIP 上限 32 MB）。 |

密钥、令牌一律不打印、不进日志；`run-stock-backup-offsite.ps1` 只把变量传给子进程，
不放在命令行里，所以不会出现在进程列表或计划任务定义中。

## 加密密钥的保管

> [!warning] 密钥丢了，云端备份就全部作废
> `backup-offsite.key` 只是 32 字节随机数，没有它任何一份云端备份都解不开。
> 它不在网盘里，也不在 Git 里。必须在**另一台设备/介质**上单独留一份。

当前密钥指纹（`key_fingerprint`，可用来确认手里那份是不是同一把）：
`5b93d22610c57fa0`（2026-09-16 生成）。用 `status` 或 `init-key` 的输出可以重新看到它。

## 授权

只需做一次，设备码模式（`authorize`），令牌加密后存在数据库 `baidu_pan_oauth_tokens` 表：

```powershell
pwsh -File scripts\windows\run-stock-backup-offsite.ps1 -Command authorize
```

输出会给出 `verification_url`（`https://openapi.baidu.com/device`）和 8 位 `user_code`，
在浏览器里登录百度账号输入即可。**设备码有效期 5 分钟**，超时重跑一次即可。

- access_token 有效期 30 天，代码会在过期前自动刷新。
- refresh_token **每次刷新只能用一次**，刷新失败必须重新走 `authorize`。
- 授权被回收或换 SecretKey 后，`status` 里的 `authorized` 会变回 `false`。

## 日常运行

```powershell
# 注册计划任务（每天 21:30，备份任务 20:30 之后）
pwsh -File scripts\windows\install-stock-backup-offsite-task.ps1

pwsh -File scripts\windows\run-stock-backup-offsite.ps1 -Command status     # 授权/配额/清单概况
pwsh -File scripts\windows\run-stock-backup-offsite.ps1 -Command upload     # 只上传
pwsh -File scripts\windows\run-stock-backup-offsite.ps1 -Command prune      # 只清理本地
pwsh -File scripts\windows\run-stock-backup-offsite.ps1 -Command nightly    # 上传 + 清理（计划任务用）
pwsh -File scripts\windows\run-stock-backup-offsite.ps1 -Command selftest   # 上传下载往返自检
```

每次运行的 JSON 结果追加到 `G:\StockPlatform\logs\stock-backup-offsite.jsonl`。
上传是**幂等**的：同一个文件只在"大小或修改时间变了、或还没记账"时才重传；
任务失败重跑不会重复上传已核验的文件。

## 还原

先从云端把备份树取回本地（会下载**最新一天**的基础备份 + 全部增量分片）：

```powershell
pwsh -File scripts\windows\run-stock-backup-offsite.ps1 fetch --dest G:\restore-from-cloud --day 2026-09-16
```

> [!warning] 用位置参数写 `--dest` / `--day`
> `pwsh -File` 会把 `-CommandArguments '--dest','G:\x'` 当成一个逗号连起来的字符串，
> 选项会被静默丢掉。位置参数写法（上面那种）才是对的。

`--day` 省略时取云端最新一天。取回后按本地流程还原到**新库**：

```powershell
pwsh -File scripts\windows\restore-stock-database.ps1 `
  -DumpFile G:\restore-from-cloud\2026-09-16\trading_hareness-2026-09-16.dump `
  -TargetDatabase trading_hareness_restore_20260916 `
  -BackupRoot G:\restore-from-cloud
```

取回时会逐个校验：下载大小 == 清单大小 → 解密（GCM 校验）→ 明文 SHA-256 == 清单 SHA-256。

## 百度网盘限制（2026-09 官方文档）

- 应用只能访问 `/apps/{应用名}`，网盘里显示为 `/我的应用数据/{应用名}`。
- 单文件：普通 4 GB / 会员 10 GB / **SVIP 20 GB**。分片：普通固定 4 MB / 会员 16 MB / **SVIP 32 MB**，分片数 ≤ 1024。
- 上传域名必须调接口获取，不能写死（代码用 `locateUploadServer`）。
- 同名覆盖用 `rtype=3`。
- 未通过上线审核的应用限 **10 次/小时**；"个人使用"类应用创建后即上线，实际额度以实测为准。

## 验证

- `feishu-adapter/stock-backup-offsite.test.mjs`：加解密往返与篡改拒绝、密钥文件、清单归类、
  本地清理分组、**用假网盘跑完整 upload/prune/fetch 并逐字节比对**。
- `feishu-adapter/baidu-pan-storage.test.mjs`：OAuth（含设备码 pending/致命错误区分）、
  流式分片上传请求序列、失败换域名重试、分片上限、配额接口。
- `selftest`：对真实网盘做 40 MB 的加密→上传→下载→解密→哈希比对，并测上传速度后删除远端文件。
