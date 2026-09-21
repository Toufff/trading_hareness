# 协作者复权维护交接

本文件面向 lightServer 上的可信协作者。owner 与 peer 维护入口已部署；版本和实测结果见文末。
范围是 StockPlatform 累计复权因子，不涉及券商下单或实盘账户操作。

## 本轮授权与方案复审

业主于 2026-09-21 授权协作者完整维护复权模块，保留现有可信读写权限，
无需逐次申请普通修复许可。不授予与本模块无关的整机管理或券商交易能力。
当前生产实测 stock_peer 已可读写因子及两张日线表，接口却声明只读；本轮纠正契约，
不是通过重跑旧 bootstrap 缩权，也不复制龙虎上游凭据。

复审发现与实施决定：

1. 文件锁不能跨机器：所有正式 sync/repair 写入口共用 PostgreSQL 会话锁，
   写事务使用持锁的同一连接；连接断开后不能换连接继续写。
2. 幂等不等于防并发：拿不到锁返回明确 busy，不伪装完成，不重复抓取上游。
3. 修复按日提交可能部分完成：变更日志与因子写入同事务，保存运行身份与终态。
4. 回滚不能覆盖后来修复：比较当前值与当次写后值，任何冲突整次拒绝。
5. Windows 默认配置路径在 Linux 无效：CLI 支持显式使用继承环境。
6. 不重建协作者旧服务镜像：部署独立版本化维护包，复用既有容器依赖/连接，
   不覆盖对方服务热修复，不增加第二套无人值守调度。
7. 宽权限是信任模式，不是字段级数据库强隔离；互斥与审计仅覆盖约定维护入口，
   直接 SQL、旧版程序及其他因子写入旁路不能声称自动受其保护。

## 验收计划

纯函数与 CLI 参数、真实隔离 PostgreSQL 迁移/审计/回滚/互斥/断线、既有因子回归、
完整后端、前端 typecheck/build、生产状态与契约、协作者真实数据库和龙虎路径、
协作者预演/正式幂等运行，分别记录证据。生产不注入假因子来证明修复能力。

## 工作检查点

- 开发基线：5181eed；独立工作区 F:/AIWorkflow/worktrees/peer-factor-maintenance。
- 原目录有其他未完成的 observation-adjusted-returns 测试，未改动。
- 开始时生产：20260921T200743-0d06d0864611-clean。
- 最终相关隔离库测试：342 passed / 5 skipped / 663 subtests；5 项为既有额外存储搬迁实验。
- 完整后端首轮发现 main.py 长度守卫，已精简接线说明，未放宽守卫。
- 最终后端 3181 passed / 127 skipped / 888 subtests；跳过项是其他模块的外部环境条件，本轮 16 个真实数据库控制测试均运行。

## 权限与责任

日常诊断、补齐、窗口重算、审计查询、受保护回滚均已获授权，不需要逐次申请许可。
可以修改本模块代码、补测试、提出迁移，通过仓库测试和发布流程交付。
现有 `stock_peer` 继承 `quant_app`，不是只读账号；因子表和两张日线表已有
SELECT/INSERT/UPDATE/DELETE/TRUNCATE 权限不变。新审计表授予 CRUD 和审计序列 USAGE/SELECT。
不要重跑历史 `bootstrap-local-peer.ps1`，其旧模板可能缩权。

| 关系 | 本流程支持的写入 |
|---|---|
| `quant.daily_adjustment_factors` | 因子、provider、available_at、raw 证据 |
| `quant.canonical_bars_daily` | 只改累计 adj_factor 投影 |
| `quant.market_bars_daily` | 只改累计 adj_factor 镜像 |
| `quant.factor_maintenance_runs` | 运行身份、版本、参数、状态、变更数量与结果 |
| `quant.factor_maintenance_changes` | 同事务的因子 before/after 证据 |

这是信任模式，不是列级强隔离、也不是防篡改审计。宽 SQL 权限客观上可绕过命令、
改其他列或修改日志；正常维护必须走下述入口。不要把“有权限”理解为能跳过因子语义、
测试与上线验收。不额外开公网 PG、不复制龙虎上游 token、不增加主机 root 或券商能力。

## 登录后立即使用

在 lightServer 以现有 `stockpeer` 身份运行，不需要 sudo 或龙虎上游凭据：

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/docker.sock
FACTOR=/home/stockpeer/factor-maintenance/current/scripts/shared-peer/factor-maintenance-peer.py
python3 "$FACTOR" probe
python3 "$FACTOR" status --lookback-days 30
python3 "$FACTOR" sync --dry-run --lookback-days 30
python3 "$FACTOR" history
```

probe 应返回 ready、stock_peer、SharedLonghuReadSource，五张表 CRUD 为 true；
它只证明连接/权限/契约，不证明因子正确，也不替代真实龙虎调用测试。
代码包只读挂载，复用 `trading-hareness-peer-quant-research-1` 正在运行的镜像 ID 和私有配置；
不执行镜像原 entrypoint，所以不迁移、不启动背景调度、不重建现有服务。
单次容器退出即删除，限制 1 GiB / 128 PID，临时目录 64 MiB。
支持 CPU CFS quota 的 daemon 另限 2 CPU；当前 lightServer rootless 不支持该能力，
probe 的 resource_limits 明确标记 CPU quota 不可用，不声称已经施加 CPU 硬限。
数据库走独立 `db-batch-tunnel:5433`，共享龙虎走 `http://db-tunnel:5681`。
凭据只进入进程/容器环境，不写 argv/Git/手册；可信 Docker 管理员仍能查看环境。
不要在工单粘贴完整 docker inspect。
peer 固定 QUANT_FACTOR_FETCH_WORKERS=4，匹配实测 owner 共享网关 4 个工作线程，
避免 16 路请求冲满 8 个队列席位、快速 503 后形成重试风暴。owner 直连默认仍为 16，
不额外加休眠或改上游速率限制。以后调整网关容量时同步复测此参数（允许 1..16）。

## 命令与结果解释

| 命令 | 写库 | 龙虎调用 | 用途 |
|---|---|---|---|
| status | 否 | 否 | 当前待处理日期、质量状态 |
| sync --dry-run | 否 | 否 | 列出夜间同步工作 |
| sync | 按日期提交 | 有待处理标的时 | 默认最近 30 天补齐 |
| repair | 否 | 有待处理标的时 | 重推并展示 plan/projection |
| repair --apply | 按日期提交 | 有待处理标的时 | 执行指定或自动识别窗口 |
| validate | 否 | 有历史样本时 | 对照存量 Tushare 校准推导 |
| history | 否 | 否 | 最近 20 次运行或指定 run_id |
| rollback | 否 | 否 | 完整性及冲突预演 |
| rollback --apply | 单事务 | 否 | 恢复一次运行前的因子 |

只读命令由服务器 default_transaction_read_only 强制执行，不靠代码自觉。
stdout 是 ASCII JSON，兼容 Windows GBK 宿主。退出码 0 完成、1 失败、3 持锁忙、2 参数错误。
status/validate 是报告命令，exit 0 不代表无缺口或完全匹配；sync 的 coverage-skipped 也可能 exit 0，
必须读 pending/skipped/reason。异常仅输出异常类型和排查方向，不倾倒含连接信息的堆栈。
所有命令可加 `--actor collaborator-name`；actor 自报，database_role 独立记录真实登录角色。
owner CLI 可用 `--env-file -` 继承环境；peer 包装器自动注入且拒绝覆盖 env-file。

## 一次修复的标准流程

先确认最新结算日，选择有证据的最小窗口，不把未收盘日/未来日期当缺口。
下列日期只是语法示例，执行前替换为实际目标：

```bash
python3 "$FACTOR" status --lookback-days 30
python3 "$FACTOR" repair --from 2026-09-18 --to 2026-09-18 --actor collaborator-name
# 先审查 plan/projection 中缺失锚点、预期 NULL、guard。
python3 "$FACTOR" repair --from 2026-09-18 --to 2026-09-18 --apply --actor collaborator-name
python3 "$FACTOR" history --run-id <maintenance_run_id>
python3 "$FACTOR" status --lookback-days 30
```

正常补齐优先 sync，重推旧窗口用 repair。预演与正式运行各自取当时证据，不是冻结的计划。
全市场可有数千个龙虎请求，不是秒级；网关最多 300 条物理批次，不要并发多个全市场预演。
验收看 completed/unchanged、repair.after.guard 和 value_mismatches 均为零；
残余 NULL 必须有明确证据缺失/锚点不足等解释，不能只看命令成功或“有因子”。

## 因子语义底线

bar.adj_factor 是累计公司行动因子，close * adj_factor 跨日可比。
不能写每日 qfq/原价比、逐日比例、占位 1；没有可靠证据就保留 NULL。
真实累计值恰好为 1 与填假 1 是两回事。当前龙虎推导 provider 为 longhu_qfq_derived，
必须显式 raw.factor_semantics=corporate_action_cumulative；superseded_at 非空的证据不可定价。
Tushare 仅作为存量锚点/校准，本命令不请求 Tushare；历史 provider 缺 semantics 的兼容规则
沿用 owner 判定式，不能自行更换枚举。manual_ 方法检查点由现有推导逻辑处理，不硬造锚点。

权威：[ADJUSTMENT_FACTOR_SEMANTICS.md](ADJUSTMENT_FACTOR_SEMANTICS.md)、
[PEER_API_CONTRACT.md](PEER_API_CONTRACT.md) 与运行中 /api/v1/peer/contract。
没有 adjustment_state 实体列，不依据手册散文推断 schema。
validate 默认 2026-06-01 至 2026-08-26，是历史校准，不是当前全市场正确性的独立证明。

## 互斥、审计与部分失败

新版 owner/peer 正式 sync、repair、rollback 共用 PostgreSQL 会话 advisory lock 72814409749381。
拿锁前不抓龙虎；拿不到返回 busy。所有写事务使用持锁的同一连接；连接断开后不能重连续写。
下一次成功拿锁会把遗留 running 标记 interrupted；单看 running 不证明进程仍活着。
这些保证不覆盖直接 SQL、旧程序、Tushare/年度导入等其他旁路写入。

SessionDatabase 在每个事务内设置 quant.factor_run_id，触发器同事务写入日志，
run.change_count 也随事务提交。触发器缺失/禁用会拒绝正式运行。
repair 按日提交，整次不是跨日期全有或全无：后一天失败时前一天可能已完成。
factor 保存完整行 before/after，bar 只保存因子；数值不变不产生审计行。
同值重算可能更新证据时间/raw，业务幂等不等于审计行数必为零。

```sql
SELECT run_id,operation,actor,database_role,source_version,status,change_count,started_at,finished_at
FROM quant.factor_maintenance_runs ORDER BY started_at DESC LIMIT 20;
SELECT table_name,row_key,before_image,after_image,recorded_at
FROM quant.factor_maintenance_changes WHERE run_id='<run-id>' ORDER BY change_no;
```

## 回滚与代码回退

```bash
python3 "$FACTOR" rollback --run-id <maintenance_run_id>
python3 "$FACTOR" rollback --run-id <maintenance_run_id> --apply --actor collaborator-name
python3 "$FACTOR" history
python3 "$FACTOR" status
```

只允许已结束的非 rollback 运行；从最近有变更的运行向前撤销，失败/中断但已有提交的运行也可以。
日志完整性、后续运行和当前值与 after 的比较全部通过才恢复，任一冲突整次拒绝；
不跳过冲突行、不提供 force 开关。恢复反向写入顺序，因子及 JSON 数字不经 float。
bar 只恢复因子，不恢复 OHLCV、不删除或创建价格 bar。回滚自身也形成新运行和审计。

恢复旧数据不保证旧数据正确；不会倒带调度回执或重建已发布扫描报告。
恢复后重新检查，必要时按普通流程 sync/repair，不能宣称所有衍生结果已经回退。
在线自动回滚上限 365 天；审计纳入冷存储，空间压力可能缩短热窗口。
只要日志归档或计数不匹配就拒绝自动回滚，交 owner 根据冷层/备份人工审核，不改日志绕过检查。

代码回退与数据回滚分开。owner 使用已有 switch-stock-release.ps1 切回前一个已验证 release；
peer 只切 /home/stockpeer/factor-maintenance/current 到保留旧包，不重建服务。
切换只影响下次启动，现有容器绑定启动时真实版本目录，先等任务结束。
普通代码回退不需要 Alembic downgrade；迁移降级只移除触发器、保留日志。
旧代码可能不受本锁保护，回退后应先停止新 peer 写入并明确维护窗口。

## 代码维护与发布

- app/adjustment_factor_maintenance.py：窗口、sync、repair、validate。
- app/longhu_adjustment_factors.py：推导、证据、推广和按日事务。
- app/factor_maintenance_control.py：同连接锁、运行记录、CAS 回滚。
- scripts/adjustment-factor-maintenance.py：跨平台 CLI 与只读依赖。
- scripts/shared-peer/factor-maintenance-peer.py：独立容器启动与 probe。
- migrations/versions/20260921_0115_factor_maintenance_audit.py：审计 schema。

新算法必须复用 managed_run/SessionDatabase，不在持锁后另开写连接；
新增 bar 因子写点必须通过 test_adjustment_factor_semantics_guard.py。
instruments 写入必须遵守 AGENTS.md 的排序及锁重试规则。
不得改生产 current/release 文件、打包凭据、重建覆盖协作者旧服务热修复。

```powershell
.\.venv\Scripts\python.exe scripts/run-isolated-db-tests.py tests/test_factor_maintenance_control.py tests/test_adjustment_factor_maintenance.py tests/test_longhu_adjustment_factors.py tests/test_adjustment_factor_semantics_guard.py
```

测试脚本只继承 PG 设置，创建 stock_audit_test_* 库并 finally 删除，不能将夹具指向生产库。
发布前：纯函数、真实隔离库事务/锁/回滚、既有推导、完整后端、前端 test/typecheck/build、
Windows 发布契约、staged secret scan。干净 commit 经标准 publish-stock-release.ps1，不加 SkipTests/AllowDirty。
owner schema/代码验收后，git archive 同源包部署 peer 并切 current，再跑 probe 和真实维护命令。
不新增 peer 定时任务：owner 收盘非阻断步骤与每日 04:30 原调度继续负责自动维护。

## 故障速查

| 现象 | 处理 |
|---|---|
| busy / exit 3 | history，等持锁任务结束；不杀会话、不绕锁 |
| failed/interrupted 且 change_count > 0 | 核对已提交日期，选择继续幂等修复或最新优先回滚 |
| probe 连接失败 | 查 batch sidecar 和 owner batch tunnel，不随意重启盘中隧道 |
| 缺权限/表/触发器 | 核对 owner 版本和迁移，禁止盲跑旧 bootstrap |
| 网关 401/403 | 私下核对共享 read key，不索要龙虎上游 token |
| 剩余 NULL | 看 plan/projection 的证据及锚点，不填 1 |
| rollback conflict / journal incomplete | 不强行覆盖；交付 run_id、日期范围、安全日志给 owner |
| owner 发布期间短暂失败 | 有界退避；锁连接断开后停止本次，不续写 |

## 发布与验收记录（2026-09-22）

### 版本与入口

- 本任务首次 owner release：`20260922T001401-cce23b4193d2-clean`，源码 `cce23b4193d2ef6c8a6b8313c900b8102a71078d`。
- 最终整合 owner release：`20260922T010409-390a7560b431-clean`，源码 `390a7560b431`；包含同日业务闭环更新与本模块，未回退任一任务。
- Schema：`20260921_0115`，三张表捕获触发器已安装；新审计冷表/全历史运维视图已安装，未修改角色超时。
- peer 当前包：`1886fcf437d7344e72d724753bec63147d3a2dc8`；相对首次 owner 包增加 rootless CPU quota 能力探测、peer 4 路并发配置；最终整合 owner 已包含这些源码，owner 默认直连行为不变。
- peer 稳定入口：`/home/stockpeer/factor-maintenance/current/scripts/shared-peer/factor-maintenance-peer.py`。
- peer 最新交接文档：`/home/stockpeer/factor-maintenance/HANDOFF.md`，可直接交给现有 stockpeer 协作者阅读。
- peer 历史包：`/home/stockpeer/factor-maintenance/releases/<commit>`；目录由 stockpeer 持有，可自行维护。
- 源码分支：`codex/peer-factor-maintenance`，已合入主工作分支的 `390a756`。与同仓库另一项业务闭环任务的整合发布由“大师教教”任务协调，不能从未合入本分支的旧 HEAD 再发布而覆盖接线。
- 最新活跃版本以 `G:/StockPlatform/current/release-manifest.json`、peer current 实际指向为准，不把本节初次发布号误当永远最新。

### 已完成的验证

| 层次 | 结果与边界 |
|---|---|
| 相关隔离 PostgreSQL | 342 passed；5 项其他存储实验跳过；16 项新 DB 场景实际执行 |
| 全后端 | 3181 passed / 127 skipped / 888 subtests；不是声称全仓库零跳过 |
| 前端 | 标准发布门禁 33 文件、138 用例通过，typecheck/build 通过；有既有 >500 kB chunk 告警 |
| 最终整合发布 | 390a756 发布门禁：3188 后端、139 前端通过；typecheck/build 通过；195 条在线 OpenAPI 一致；共享端到端验证通过 |
| Adapter | 116 / 116 通过 |
| Windows | 标准发布契约通过；另单独执行复权 04:30 任务契约和存储 tier wiring |
| API | 首次上线 OpenAPI 194 路径与该版本 generated.ts 一致；无新增 HTTP 写接口 |
| Shared runtime | 首次 00:54:53、整合发布后 01:10:25 均 verified：owner/peer health 200、真实行情、契约、完整股票接口及 301 分页；凭据未回显 |
| 权限 | 真实 stock_peer 登录，经 batch:5433，五张维护关系 CRUD 全部可用；SharedLonghuReadSource 正确选中 |
| 跨机器互斥 | owner 持锁时 peer 正式 sync 返回 busy / exit 3；未触发重复抓取/写入 |
| 正式 repair | 自动识别窗口返回 unchanged；没有损坏日期或未标注占位证据，未人为制造修复 |
| rollback | 上述零变更运行预演 planned、正式回滚 completed；数据恢复细节由隔离 DB 测试证明，非生产造假因子演练 |
| sync | 7 日预演成功；0 日空窗口正式入口 unchanged，验证运行登记，不冒充真实补齐 |
| validate | 在只读输入边界抽取 5 只真实历史股票，CLI validate 调真实共享龙虎；5 step pairs，fetch_errors=0，累计相对误差=0；该样本无公司行动，不外推全市场算法精度 |
| 全市场指定日 repair 预演 | 9 月 21 日真实只读预演 planned：5,420 标的、5,413 推导、5,395 不变、18 可补空值；龙虎错误 0，存量冲突 0，预期值不一致和占位泄漏均 0；7 个指数无因子链保留 NULL |

最新 status 读回：最近 5 个结算日达到覆盖阈值，pending_dates=[]；两张日线的占位因子泄漏和
因子值不一致均为 0。达到覆盖阈值不等于每个股票都已补齐：9 月 21 日 status 分母 5,420、
有效因子 5,395（99.5387%），保留诚实缺口，不能把“complete”理解成 100% 覆盖。
owner/adapter 本地 health 均 200；未认证公网 /api/config 返回 401，属于认证边界检查，
不冒充已登录网页的端到端验收。

全市场回执：peer `/home/stockpeer/factor-maintenance/receipts/20260922-full-preview.json`，
owner 证据目录 `factor-peer-full-preview.json`；stderr 为 0 字节，运行约 10 分钟。
预演投影 NULL 从 25 降至 7，但本次没有执行这个窗口的 apply，生产仍保留原值。
18 条为可补齐候选，不是现有值错误；7 个指数为 000001.SH、000300.SH、000688.SH、
000852.SH、000905.SH、399001.SZ、399006.SZ，原因均 no_factor_lineage。
下一次需要补这批空值时，先重新预演再按正常授权执行 repair --apply；
不要把本次 planned 当成已经落库，也不要为达到零 NULL 给指数填占位 1。

隔离数据库覆盖：数值和 raw JSON 高精度恢复、插入/更新回滚、同一行多次写入反向恢复、
事务失败不留变更、部分日期提交后运行失败、不同连接抢锁、断线释放锁与禁止重连续写、
遗留 running 清理、只读预演、缺触发器拒绝、重复值不膨胀日志、后续运行阻止旧回滚、
最新优先回滚链、直接改值冲突整次拒绝、归档/缺失日志拒绝部分回滚，以及生产入口实际调用 guard。

生产审计示例：

- repair：`f25a5aaa-2947-4385-8f72-31096b5396d4`，初始 unchanged，回滚后 rolled_back，change_count=0。
- rollback：`1a719f08-f1b7-4f22-a0ca-d9cb7716cddc`，completed，change_count=0。
- 空窗口 sync：`9ed1d47f-b47c-4f63-8b46-1099a052779c`，unchanged。
- database_role 均为 `stock_peer`，source_version 为 peer bundle 的真实版本目录。

### 首轮暴露的问题及处置

1. 新隔离库夹具缺 available_at、旧测试假依赖缺 control：修正夹具及兼容接线，未放松生产校验。
2. main.py 长度守卫、storage tier 索引/表数守卫：精简接线说明、补新表明确断言，不抬高架构上限。
3. 懒加载前端测试过早断言：等真实 dynamic import 完成，不改业务 UI 来凑测试。
4. 干净 release clone 缺 GUI host 编译产物：先标准 build，再完整发布；未用 SkipTests。
5. rootless Docker 不支持 CPU CFS quota：能力探测后跳过该一项，保留内存/PID限制并在 probe 明示。
6. 16 路远端预演导致 owner 4 worker / 8 queue 饱和，503 也被旧 peer 转成 500：实测错误正文为 capacity saturated；改 peer 为 4 路，完整共享验收恢复 verified。失败预演未写因子。
7. 发布保留清理中有旧 GUI host 被占用的告警，保留原目录延后清理；未强杀或删除正在使用的可执行文件。标准保留策略清理 `20260921T163439-d9e8b8670ea5-clean`，可由 Git 源码重建；前一版仍保留用于代码回退。
8. 全市场进程在 Docker 事件中 exitCode=0，但原长 SSH 连接未收回 stdout：关闭仅属于本次只读验收的滞留 SSH 客户端，改为服务器本地持久化结果。后续长任务也建议用下述方式保留回执，避免把终端连接当唯一证据。

### 长任务的耐断线执行

在 peer 主机 stockpeer 的交互 shell 中（沿用前面的 FACTOR / Docker 环境）：

```bash
mkdir -p /home/stockpeer/factor-maintenance/receipts
RECEIPT=/home/stockpeer/factor-maintenance/receipts/repair-$(date +%Y%m%d-%H%M%S).json
nohup python3 "$FACTOR" repair --from 2026-09-21 --to 2026-09-21 --actor collaborator-name >"$RECEIPT" 2>"$RECEIPT.stderr" < /dev/null &
echo "$! $RECEIPT"
# 完成后读取回执；这是只读预演，实际日期应根据待修复证据选择。
```

SSH 建议加 `-o ServerAliveInterval=15 -o ServerAliveCountMax=3`。不要仅因 SSH 断开就重复启动写任务；
先查 history、持锁状态和已有进程。正式写任务的 DB 审计比 SSH stdout 更可靠。
本轮完整证据归档于 owner `G:/StockPlatform/data/research/peer-factor-maintenance-20260922`，不含私有 env 文件。

未声称的验收：未在生产写假因子测试破坏性回滚，未等到下一次 04:30 自然触发，
未验证每个外部写入旁路都遵守新锁，未将这次维护测试当作策略盈利或交易建议验证。
