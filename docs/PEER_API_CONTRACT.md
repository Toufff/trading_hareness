# owner 对外契约与错误回流（peer-contract-v1）

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
| `enumerations.factor_semantics` | `raw->>'factor_semantics'` 的实时取值集合 |
| `not_provided[]` | **不存在且不会添加**的东西，逐条给出理由 |
| `endpoints[]` | 允许调用的 owner 接口 |
| `rules[]` | 三条使用规则（见下） |

`objects` 是一份**刻意收窄的清单**（`SUPPORTED_OBJECTS`），不是该角色能读到的全部。`stock_peer` 继承 `quant_app`，能 SELECT 200 多个关系，其中绝大多数不属于任何约定；把它们全发布出去，等于在更高一层重演原来的问题——暗示"今天能读到的就是被支持的"。

`not_provided` 的每一条都对应 9/19 那次启动门里一条真实失败的检查。保留它们的名字，是为了让这份契约**既能当目录用，也能当反驳用**：对方可以直接删掉检查，而不是等一个永远不会来的迁移。

三条规则（写在响应里）：

1. 只能对 `objects[]` 和 `enumerations[]` 里的东西做断言。不在本文档里的，即使当前能读到，也不属于约定。
2. 永远不要从交接文档的散文里推断 schema。本契约与任何文档冲突时，以本契约为准。
3. **一个阻断性启动检查，必须先对本契约跑通过至少一次，才允许它阻断。** 先跑 report-only。

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

## 代码位置

- `quant-service/app/peer_contract.py` —— 契约内容与实时自省
- `quant-service/app/peer_error_feed.py` —— 日志解析与聚合
- `quant-service/app/routers/peer_support.py` —— 两个接口与鉴权
- `quant-service/tests/test_peer_support.py` —— 27 个用例，日志样本取自 2026-09-20 生产日志
- 配置：`QUANT_POSTGRES_LOG_DIR`（默认 `G:/StockPlatform/logs`）、`QUANT_PEER_CONSUMER_ROLES`（默认 `stock_peer`）

## 改契约的规矩

新增或移除 `SUPPORTED_OBJECTS` 一项，等于改变对外承诺：必须同步更新本文件，并在发布前通知协作方。`not_provided` 只增不减——删掉一条，意味着 owner 决定提供它，那是一次真正的契约变更。
