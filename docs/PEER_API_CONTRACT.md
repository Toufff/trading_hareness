# owner 对外契约与错误回流（peer-contract-v2）

状态：2026-09-20 上线。本文件是 `/api/v1/peer/contract` 与 `/api/v1/peer/errors` 两个接口的合同；实现与本文冲突时先改本文再改代码。

## 为什么有这两个接口

2026-09-19 协作方（peer）新 release 启动失败。直接原因是它的启动门断言了一套 owner 从未有过的 schema：交接文档里把 `adjustment_state` 描述成"健康面板按日计算出的四态值"，被读成"三张表上应该有这个列"；因子语义被读成一个叫 `factor_semantics` 的列；冷层被读成五张 `_cold` 后缀的行情表；还硬写了一个 owner 从未写过的取值 `cumulative_tushare`。

这些不是"文档读得不够仔细"能避免的。**散文必然被推断补全**，唯一的解法是把真实的东西直接发布出去。

同一天还查明第二件事：peer 那条 `interval $2` 的语法错误，9/19 一天在 owner 的 PostgreSQL 日志里刷了 5123 行，而 peer 自己的容器日志 40 分钟内 1131 行全是 INFO，零 ERROR。PostgreSQL 把错误原样返回给了客户端，是 peer 的代码把异常吞了。但即使它把异常打出来，**"这条错误发生在别人的库里、一天五千次"这个事实只有 owner 这边知道**。

所以：一个接口回答"这里到底有什么"，另一个回答"我弄坏了什么"。

## 鉴权与边界

两个接口都用 peer 已有的共享只读密钥（`X-Quant-Read-Key`，对应 `QUANT_SHARED_READ_API_KEY`）。

错误回流额外限制角色：`QUANT_PEER_CONSUMER_ROLES`（默认 `stock_peer`）是一个允许清单，请求其他角色返回 403。**密钥不是读取 owner 自身语句的许可**——owner 的 SQL 语句里可能嵌有数据。

两个接口都是只读：契约走目录读 + 一次有界 DISTINCT，错误回流只读日志文件，不写库、不改任何状态。

## GET /api/v1/peer/contract

参数：`role`（可选，须在允许清单内，默认清单首项）。

响应要点：

| 字段 | 含义 |
|---|---|
| `alembic_head` | 生产库当前迁移版本，pin 检查以它为准 |
| `objects[]` | 被支持的关系，含真实列（类型 / nullable / default）、索引定义、表空间、该角色的实际权限 |
| `cold_tier` | 真正在 `stock_cold` 表空间里的表；`peer_readable: false` |
| `enumerations.factor_semantics` | `raw->>'factor_semantics'` 的实时取值集合，**以及覆盖率**（总行数、带该键的行数、该键最早出现的交易日） |
| `derived_rules[]` | 答案不是某一列的问题。v2 只有一条：`adjustment_factor_usable` |
| `not_provided[]` | **不存在且不会添加**的东西，逐条给出理由 |
| `endpoints[]` | 允许调用的 owner 接口 |
| `rules[]` | 三条使用规则（见下） |

`objects` 是一份**刻意收窄的清单**（`SUPPORTED_OBJECTS`），不是该角色能读到的全部。`stock_peer` 继承 `quant_app`，能 SELECT 200 多个关系，其中绝大多数不属于任何约定；把它们全发布出去，等于在更高一层重演原来的问题——暗示"今天能读到的就是被支持的"。

`not_provided` 的每一条都对应 9/19 那次启动门里一条真实失败的检查。保留它们的名字，是为了让这份契约**既能当目录用，也能当反驳用**：对方可以直接删掉检查，而不是等一个永远不会来的迁移。

规则（写在响应里）：

1. 只能对 `objects[]`、`enumerations[]`、`derived_rules[]` 里的东西做断言。不在本文档里的，即使当前能读到，也不属于约定。
2. **不要对缺失的 JSON 键 fail closed。** 缺失在这里是一个被记录的状态，不是故障；`coverage` 会说明它有多普遍，能不能用由派生规则回答，不由"键在不在"回答。
3. 永远不要从交接文档的散文里推断 schema。本契约与任何文档冲突时，以本契约为准。
4. **一个阻断性启动检查，必须先对本契约跑通过至少一次，才允许它阻断。** 先跑 report-only。

### derived_rules.adjustment_factor_usable（2026-09-20 新增）

v1 发布了因子语义**在哪里**就停住了，这是同一个缺陷退了一层。实测：

- `quant.daily_adjustment_factors` 共 4,158,486 行，**只有 97,187 行（2.3%）带 `factor_semantics` 键**，该键最早出现在 2026-08-21。9/17、9/10 有一半以上的行没有它，连最近一个结算日 9/18 也还有 19 行没有。
- 另有 **61,614 行带 `superseded_at` 标记**——被后来的决策取代的行，仍作为"厂商当时发布了什么"的证据留着，但永远不能拿来定价。v1 从没提过这个键。
- 一行能不能给日线当 `adj_factor`，**取决于 provider 不亚于取决于语义**：`longhuvip_composite` 在表里可见，但任何情况下都不得用它定价。

所以"缺语义就 fail closed"会否掉表里的绝大多数，而"只看语义"会用上被取代的行。契约现在直接把 owner 自己的判定式发出来：

```sql
(((factor.provider LIKE 'tushare%' AND coalesce(factor.raw->>'factor_semantics','') IN ('','corporate_action_cumulative'))
  OR (factor.provider = 'longhu_qfq_derived' AND coalesce(factor.raw->>'factor_semantics','') = 'corporate_action_cumulative'))
 AND factor.raw->>'superseded_at' IS NULL)
```

这段 SQL 由 `app/tushare_normalization.promotable_factor_evidence_sql()` 生成，和 owner 自己用的是同一份实现，**不可能漂移**。读三个输入：`provider`、`raw->>'factor_semantics'`、`raw->>'superseded_at'`。tushare 系路由缺语义＝旧口径的累计因子，可用；派生 provider `longhu_qfq_derived` 必须显式写明 `corporate_action_cumulative`；其余 provider 一律只是证据，不是价格。

## GET /api/v1/peer/errors

参数：`role`（同上）、`since` / `until`（ISO 8601，naive 值按集群本地时区 Asia/Shanghai 解释）、`limit`（1–200，默认 50）。

默认窗口为最近 24 小时；窗口上限 31 天（`MAX_WINDOW_DAYS`），超过返回 422。单次请求最多扫描 64 MB 日志（`DEFAULT_MAX_BYTES`），超出则跳过该文件并置 `truncated: true`——宁可给一个有界的答案，不阻塞服务。

响应按"一个问题一行"聚合：

```json
{
  "role": "stock_peer",
  "total_entries": 5123,
  "distinct_problems": 1,
  "groups": [{
    "sqlstate": "42601", "level": "ERROR", "count": 5123,
    "message": "syntax error at or near \"$2\" at character 122",
    "statement": "SELECT count(*) FROM quant.intraday_board_flow_snapshots WHERE ...",
    "first_seen": "...", "last_seen": "...", "applications": ["[unknown]"]
  }],
  "truncated": false
}
```

聚合键是 `(level, sqlstate, 归一化后的 message)`。**语句刻意不参与分组**：PostgreSQL 在错误发生于语句之外时不写 STATEMENT 行，扩展协议也可能每个 prepared statement 只记一次，按语句分组会把同一个重复故障劈成"带 SQL"和"不带 SQL"两半，毁掉唯一有价值的那个计数。语句改为按组收集，最多保留 3 条不同的（`statements[]`）。

message 里的数字会被归一化（字符偏移、pid、行数几乎都是位置性的），但返回的 `message` 仍是第一次真实观测到的原文，不是掩码。

## 归属是怎么实现的

前提是 2026-09-20 11:19 对生产库做的一次 `log_line_prefix` 变更（sighup 生效，无需重启、不断连接）：

```
默认   '%m [%p] '
改为   '%m [%p] %q%u@%d app=%a %e '
```

`%q` 让后台进程的行不带会话字段，所以**无法归属的行会被直接丢弃，不会被当成某个消费方的错误交出去**——这正是整套机制要杜绝的那种推断。`%e` 给出 SQLSTATE，让错误可按错误码分类处理。

该设置由 `scripts/windows/postgres-managed-config.psm1` 的 `Get-StockPlatformManagedSettings` 生成，受 `scripts/windows/tests/test-postgres-storage-tier-wiring.ps1` 保护，不会在重新生成配置时丢失。

`attribution_available_since` 字段记录归属开始生效的时刻；早于该时刻的日志行没有角色信息，不会出现在任何人的回流里。

## 发布公告通道（2026-09-20 新增）

### 为什么不能用 HTTP 公告

2026-09-20 实测三次发布：`quant-api`（peer 用的 HTTP 面）每次停 **9.3 秒**；共享隧道三次里有两次重启，停 **5–7 秒**；**PostgreSQL 一次都没重启**。

十五秒对一个知道"对方在发布"的消费方是无感的，对一个不知道的就是一次静默丢失。而这件事没法用 HTTP 告诉它——**HTTP 正是那个会消失的东西**。数据库不会，所以公告写在表里。

### quant.owner_deploy_events

| 列 | 含义 |
|---|---|
| `deploy_id` | 把一次发布的各个阶段串起来 |
| `phase` | `starting` / `completed` / `failed` |
| `release_id`、`git_sha` | 这次发的是什么 |
| `surfaces` | `{"http_api": true, "shared_tunnel": false}`——本次会中断哪些通路 |
| `expected_seconds` | 由 surfaces 推出的预计窗口 |
| `recorded_at` | 时点 |

时序是关键：`starting` 在**隧道去留已经决定之后、第一次 stop 之前**写入。晚一步，消费方可能已经断线，读不到了；早一步，就不知道数据库通路会不会一起断。

表是只追加的。一次被杀掉的发布会留下一个没有终结行的 `starting`——那是事实，不是缺口。

`SELECT` 显式授予 `stock_peer`。它今天通过 `quant_app` 成员身份也能读到，但一个依赖隐式继承的公告通道，正是那种会悄无声息失效的东西。

### 公告不许成为发布失败的原因

`record()` 返回结果对象而不抛异常，解释器缺失或数据库连不上都只警告、发布继续。**一个被自己的记账卡住的发布，比一个没公告的发布更糟。**

### 发布窗口

两个窗口对发布关闭，在测试和构建**之前**检查（四分钟之后才拒绝，只会训练操作者条件反射地加 `-IgnoreDeployWindow`）：

- **交易时段** 09:15–15:10（交易所交易日）。提前到 09:15 是因为集合竞价已经在跑；延后到 15:10 是因为盘后采集还在收尾。
- **协作方批量窗口** 06:30–08:00（工作日），他的每日回补在这个槽位。

`-IgnoreDeployWindow` 可以覆盖——必须盘中上的修复是真实存在的——但覆盖是显式的，拒绝信息里会写明踩的是哪个窗口。

交易所日历对没有核验过的年份拒绝猜测；在这里那会让所有发布都被挡住，所以未核验年份退回"工作日视为交易日"并在理由里说明：**这个门只会往关的方向猜，不会往开的方向猜。**

## 代码位置

- `quant-service/app/peer_contract.py` —— 契约内容与实时自省
- `quant-service/app/peer_error_feed.py` —— 日志解析与聚合
- `quant-service/app/routers/peer_support.py` —— 两个接口与鉴权
- `quant-service/app/owner_deploy_events.py` —— 发布公告的写入与读回
- `quant-service/app/deploy_window.py` —— 发布窗口策略
- `quant-service/migrations/versions/20260920_0107_owner_deploy_events.py` —— 公告表
- `scripts/deploy-announce.py` —— 发布脚本调用的 CLI（`check-window` / `announce` / `latest`）
- `quant-service/tests/test_peer_support.py`、`tests/test_deploy_window_and_announcements.py` —— 用例，日志样本取自 2026-09-20 生产日志
- `scripts/windows/tests/test-stock-release-safety.ps1` —— 公告时序与窗口门的静态守卫
- 配置：`QUANT_POSTGRES_LOG_DIR`（默认 `G:/StockPlatform/logs`）、`QUANT_PEER_CONSUMER_ROLES`（默认 `stock_peer`）

## 改契约的规矩

新增或移除 `SUPPORTED_OBJECTS` 一项，等于改变对外承诺：必须同步更新本文件，并在发布前通知协作方。`not_provided` 只增不减——删掉一条，意味着 owner 决定提供它，那是一次真正的契约变更。
