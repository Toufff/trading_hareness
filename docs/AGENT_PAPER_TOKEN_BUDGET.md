# 模拟盘 token / 额度预算

面向后续接手模拟盘运维的人或 agent（预期是 Codex）。记录 `agent_paper` 三条订阅 backend 的实际消耗结构、已做的削减、**已否决的方案及其理由**，以及踩过的坑。

数据基准：2026-09-18（`agent-claude-opus` 46 轮、`agent-dsh` 34 轮 + 5 轮失败），2026-09-20 复核。2026-09-21 用户明确要求把 Claude 对照账户从 Opus 切换为 Sonnet；账户键保留以延续原资金、持仓和历史曲线。

---

## 0. 先读这一条：订阅 ≠ API 计费

`claude_cli` backend 用机主 Claude 订阅的 OAuth；`codex_cli` 用本机 `codex login` 已保存的 ChatGPT 登录。两者都不是 API backend。Codex 传输还会主动从子进程环境删除 `OPENAI_API_KEY` 和 `CODEX_API_KEY`，防止计划任务意外转为 API 计费。

由此有三个必须记住的推论：

1. **CLI 报的 `total_cost_usd` 是名义值，不是账单。** 它表示"如果走 API 会值多少钱"。没有人被收过这笔钱。本文所有美元数字都只用来做**相对比较和排序**，不要当成省下来的现金。
2. **真正被消耗的是订阅额度，而模拟盘和机主自己的交互会话共用同一个额度池。** 交易时段每轮决策都在和机主本人的工作抢配额。缓存写入的 2× 倍率在订阅额度上如何计量，未经确认，不要假设。
3. **不要为了"省钱"把 backend 换成 Messages API。** 那会把现在不产生账单的消耗变成按 token 真实扣费。见 §4「已否决」。

---

## 1. 实测消耗结构（2026-09-18，Opus 46 轮）

| 项 | token | 单价 | 名义成本 |
|---|---:|---|---:|
| 1 小时 TTL 缓存写入 | 954,964 | $10/M（2×） | **$9.55（90%）** |
| 缓存读取 | 103,290 | $0.5/M（0.1×） | $0.05 |
| 输出 | 40,169 | $25/M | $1.00 |
| 合计（重建） | | | ≈$10.61 |
| 合计（库内 `total_cost_usd`） | | | $11.50 |

差额约 $0.89 未能归因，推测是 CLI 自身脚手架 token 记在别的字段。

**核心病灶：`cache_read / cache_write = 0.108`。** 写进缓存的内容有 89% 从未被读回。

这不是单日波动：9/17 同一比值为 **0.101**（reads 16,107 / writes 159,364）。两天一致，说明是结构性的。

原因在 `agent_paper/context.py`：稳定内容和易变内容拌在同一个 JSON 里从 stdin 送入，每轮整块变化。前缀匹配一失效，Claude Code 只能把整个约 22k token 以 1 小时 TTL（写价翻倍）重写一遍，而只有系统提示那约 2.4k 能读回来。**在为一份永远读不到第二次的缓存付双倍价钱。**

同样的 token 不走缓存按 1× 输入价计只要 $4.77 —— 但 `claude -p` 不暴露 `cache_control`，调用方无法干预。

### 上下文体积分布（单轮，约 30,342 字符）

| key | 字符 | 占比 | 轮间是否变化 |
|---|---:|---:|---|
| `detail_symbols` | 13,811 | 43.3% | 变 |
| `platform_recommendation_pool` | 5,111 | 16.0% | **日内不变** |
| `platform_intraday_strategy_scan_today` | 3,527 | 11.0% | **日内基本不变** |
| `news_research` | 2,406 | 7.5% | **日内基本不变** |
| `post_close_candidates_*` | 1,691 | 5.3% | **日内不变** |
| `board_flow` | 1,417 | 4.4% | 变 |
| 其余 11 个 key | 2,379 | 7.4% | 混合 |

**约 39% 的上下文在一天内根本不变，却每轮重写一次。**

### 另一个独立问题：过采样

`runner.py` 的 `decision_minutes` 原默认为 5。9/18 的 46 轮里 **42 轮（91%）`orders` 为空**，只有 4 轮真的下单。对一个隔夜持仓的模拟盘，5 分钟一轮是严重过采样。

DSH 同日 34 轮里 21 轮空单（62%）—— 它因为响应慢（单轮 100–200s）被动降低了实际频率，过采样没那么严重，但依然偏高。

`server_tool_use` 46 轮累计 **0 次调用** —— `WebSearch`/`WebFetch` 挂着从未被用过。

---

## 2. 已做的削减（2026-09-20）

| # | 改动 | 位置 | 效果 |
|---|---|---|---|
| A | `decision_minutes` 5 → 15 | `scripts/windows/run-agent-paper-trader.ps1` 新增 `-DecisionMinutes` 参数 | 46 轮 → 约 16 轮，**约 −65%** |
| B | 清空 `AGENT_PAPER_TOOLS` | 同上 | 实测 −10% 固定前缀，整轮约 −2% |
| C | 连续失败熔断 | `agent_paper/runner.py` | 额度耗尽后不再无限空转 |
| D | 完整明细只保留持仓、挂单、人工计划；纯 focus 最多 5 只且压缩分钟/日线 | `agent_paper/context.py` | 用历史同轮数据回放：Opus context −7.3%；DSH −16.9%，DSH 的 `detail_symbols` −37.3% |
| E | DSH 用 `AGENTS.md` 首轮注入完整上下文，并用专用 patch 隐藏 coding/web/subagent 等工具 schema | `model.py`、`dsh-paper.patch.yml` | 不再读文件或因截断重读；最小实测由历史 100–200s 降至 **3.9s** |
| F | 每轮把总字符数、分块字符数、明细级别和 backend token/字符用量写进 `usage` | `runner.py`、`model.py` | 后续能直接按轮量化优化，不再凭感觉 |

**A 必须对三条 backend 同时生效。** 当前比较的是 Sonnet、DSH 与 Codex，同一交易日必须共用 15 分钟决策间隔，否则收益和 token 对照都失真。

### Claude 对照账户切换为 Sonnet（2026-09-21）

- 稳定账户键仍为 `agent-claude-opus`，不迁移主键，不重置现金、持仓、挂单或历史净值。
- 计划任务显式传入 `claude-sonnet-5`；`claude_cli` 的代码默认值也改为同一模型，避免日后重装任务时意外回退到 Opus。
- 前端显示名改为 `Claude Sonnet`；历史决策仍按各自行上的 `model` 保留原 Opus 归属，新决策记录 Sonnet。
- 本机 Claude Code 2.1.278 的最小真实调用确认 `sonnet` 别名解析为 canonical model `claude-sonnet-5`。同一输入 token 规模下，CLI 报告的名义费率约为 Opus 的 40%，因此预期显著降低订阅额度压力；订阅额度如何折算由供应商控制，不能把 60% 名义降幅承诺成精确的额度降幅。

### 熔断的语义（`runner.run_day`）

- 连续 `max_consecutive_failures`（默认 3）轮 `model_failed` → 停止调用模型，记一条 `decisions_halted`
- **不退出进程，循环继续跑到 15:01。** 挂单继续撮合、NAV 继续快照、持仓完全不动
- `day_end` 摘要带上 `halted` 与 `halted_after_consecutive_failures`
- 中间任意一轮成功即清零计数

**为什么不退出进程：** 计划任务 `MultipleInstances=IgnoreNew`，每 10 分钟触发一次。运行中的进程占着槽位，新触发被忽略。一旦退出，下一次触发会起一个全新的 `run_day`，又烧 3 次尝试。**退出比不退出更糟。**

**恢复方式：** 手动重启日内进程会重新武装熔断（再给 3 次机会）。Windows 上仅 `Stop-ScheduledTask` 可能留下占用单实例锁的 Python 子进程；重启前须核对并结束**仅该 account_key** 的旧进程，再启动任务并等待一轮成功决策。不要清空锁文件或重启其他账户。

### 新增 Codex 对照账户（2026-09-20）

- account：`agent-codex-sol`
- backend / 当前模型：`codex_cli` / `gpt-6-sol`，reasoning effort=`high`（2026-09-23 从 `gpt-5.6-sol` 切换；历史决策保留各自原模型）
- 认证：`codex login status` 回读为 ChatGPT 登录；最小真实调用 10.2s 成功、空订单
- 基线：`citics-primary` 最新 `verified_exact` 快照（2026-09-18 15:10），起始权益 **98,996.26**；四只持仓数量、成本和可卖数量已读回一致
- 注意：券商快照的显示现金 125.72 与 `总资产 - 持仓市值` 204.26 相差 78.54。现有基线规则优先保证总资产可对账，因此模拟账本现金是 **204.26**，并在 baseline 同时保留两个原值
- 计划任务：`trading-hareness-agent-paper-trader-codex`，每日 09:20 启动、每 10 分钟容灾重触发、模型决策间隔仍为 15 分钟；首次运行 2026-09-21 09:20
- 2026-09-23 14:22 恢复记录：与桌面端 11:02 更新同期，长驻进程连续三次 `cli_unavailable: FileNotFoundError` 触发熔断；旧版本化 `codex.exe` 路径失效是基于时间和现存安装目录的推断。只重启 Codex 账户并清理它遗留的两个 Python 子进程后，14:22 的 `gpt-6-sol/high` 决策成功。传输层现对进程启动时的 `FileNotFoundError` 重新定位 CLI 并仅重试一次；显式指定的二进制不会被替换。

---

## 3. 待办，按性价比排序

1. **把日内不变的块挪进 `--system-prompt`** —— 见 §5，是个**未验证的假设**，但验证很便宜。先用新增的 `usage.context_metrics.parts_chars` 跑一天再决定。
2. **失败汇总推送**：盘后扫 `agent_paper_decisions` 里 `status != 'decided'` 的行，有就推。现在这些只落 jsonl 和库，**没有任何告警** —— 9/18 DSH 丢了 5 轮（含 09:30 开盘第一轮），没人知道。
3. **`agent_paper` 仍没有组合级风控**：`rules.py` 只有 `MAX_ORDERS_PER_DECISION=10` 和 `MAX_FOCUS_SYMBOLS=5`。没有止损、回撤上限、单票仓位上限、单日亏损上限。与 token 无关，但接手运维前应当知道。

---

## 4. 已否决的方案

| 方案 | 否决理由 |
|---|---|
| 换 Messages API 直连以控制 `cache_control` | **需要 API key，会把名义消耗变成真实账单。** 这是方向相反的"优化" |
| 按上述重排上下文、精确放缓存断点 | 依赖上一条，一并否决 |
| 运维自行换更小的模型（Sonnet / Haiku） | 2026-09-20 时否决，因为当时目标是 Opus 对 DSH；2026-09-21 用户明确改为 Sonnet，已覆盖该旧决定。以后仍不得由运维自行降级 |
| 调 `effort` 降低思考深度 | 输出只占成本 8%，不值当 |
| Batch API（五折） | 需要实时决策，不适用 |

---

## 5. 未验证的假设：把稳定内容挪进 `--system-prompt`

**观测依据：** 每轮 `cache_read_input_tokens` 稳定在约 2,394，恰好约等于当前 `SYSTEM_PROMPT` + `OUTPUT_SCHEMA` 的规模 —— 说明系统提示这一段确实被缓存住且每轮真的读回来了，只有 stdin 那 22k 在反复重写。

**推论：** 把 §1 表里标"日内不变"的块（约 39%）拼进 `--system-prompt`，它们就进入那个能读回来的区域，stdin 只留行情、账户、时间。

**这是推断，不是结论。** 验证方法：挪一块 → 跑一个交易日 → 看 `agent_paper_decisions.usage` 里 `cache_read_input_tokens` 有没有相应涨上去。涨了成立，没涨就退回。

不要跳过验证直接整体重构。

---

## 6. 坑

### `--tools` 是限制器，不是开关 —— 删掉它会让消耗暴涨

2026-09-20 实测，同一个 `model-check` 请求三种形态：

| 形态 | 名义成本 | duration |
|---|---:|---:|
| `--tools ""` | **$0.0405** | 15.0s |
| `--tools WebSearch,WebFetch` | $0.0452 | 15.9s |
| **省略 `--tools`** | **$0.2508** | 14.5s |

省略 flag 不等于"没有工具"，它等于**恢复 CLI 的全套默认工具及其说明**，前缀涨到 6 倍。

所以「不要工具」的正确写法是 `--tools ""`，`ClaudeCliModel.command()` 里必须无条件送出这个 flag。这个错误我在 2026-09-20 犯过一次并当场回退，代码和测试里都留了注释。

### 1 小时 TTL 在这里是最差选择

写价 2×，而前缀每轮就变，读回率只有 10.8%。在能控制 TTL 的场合（目前控制不了），5 分钟 TTL 严格更优。

### 凭据是共享的

模拟盘跑 `claude -p` 用的就是机主自己那份订阅凭据。任何提高轮次或上下文的改动，都会直接挤占机主本人的可用额度。

Codex 同理使用机主 ChatGPT/Codex 额度。`codex_cli` 必须继续带 `--ephemeral --ignore-user-config --ignore-rules --sandbox read-only`，并使用临时目录；不要让项目规则、历史会话或写工具混进每轮请求。

### DSH 的上下文不是普通输入文件

DSH 的 `agent-instructions` 会在首轮自动注入工作目录下的 `AGENTS.md`。模拟盘有意利用这个机制一次性注入规则、schema 和 context；不要改回 `instructions.md + context.json` 再让模型调用文件工具读取。`DSH_CONTEXT_MAX_BYTES=60000` 是对 65,536-byte 注入上限的安全余量，超过时应 fail closed，而不是静默截断。

### 模拟盘整条链跑在开发 checkout

计划任务直接指向 `F:\AIWorkflow\trading_hareness\scripts\windows\run-agent-paper-trader.ps1`，它再调同一 checkout 的 `.venv` 和 `quant-service/app/agent_paper/`。**改动不需要发布到 G:，但长驻 Python 进程已加载的模块要等重启后才生效**；宿主 exe 来自 `G:\StockPlatform\current`，所以改完必须提交。

---

## 7. 复测命令

```powershell
# 连通性 + 成本探针（一次真实调用，会消耗额度）
pwsh -NoProfile -File F:\AIWorkflow\trading_hareness\scripts\windows\run-agent-paper-trader.ps1 `
  -Command model-check -Backend claude_cli

# Codex / DSH 探针
pwsh -NoProfile -File F:\AIWorkflow\trading_hareness\scripts\windows\run-agent-paper-trader.ps1 `
  -Command model-check -Backend codex_cli -Model gpt-6-sol -ReasoningEffort high
pwsh -NoProfile -File F:\AIWorkflow\trading_hareness\scripts\windows\run-agent-paper-trader.ps1 `
  -Command model-check -Backend dsh

# 单元测试（必须从 quant-service 运行，含熔断、Codex、DSH）
Set-Location F:\AIWorkflow\trading_hareness\quant-service
..\.venv\Scripts\python.exe -m pytest tests/test_agent_paper.py -q

# 计划任务契约（只临时注册/回读，不调用模型）
Set-Location F:\AIWorkflow\trading_hareness
pwsh -NoProfile -File scripts\windows\tests\test-agent-paper-task.ps1
```

按日统计实际消耗与空单率：

```sql
SELECT account_key, trading_date, status, count(*) AS n,
       sum((usage->>'total_cost_usd')::numeric) AS notional_usd,
       count(*) FILTER (WHERE output->'orders' = '[]'::jsonb) AS empty_rounds
  FROM quant.agent_paper_decisions
 GROUP BY 1, 2, 3 ORDER BY 2 DESC, 1;
```

缓存效率（这个比值是首要健康指标，越接近 0 越浪费）：

```sql
SELECT trading_date,
       sum((usage->'usage'->>'cache_read_input_tokens')::bigint) AS reads,
       sum((usage->'usage'->>'cache_creation_input_tokens')::bigint) AS writes,
       round(sum((usage->'usage'->>'cache_read_input_tokens')::bigint)::numeric
           / nullif(sum((usage->'usage'->>'cache_creation_input_tokens')::bigint), 0), 3) AS read_write_ratio
  FROM quant.agent_paper_decisions
 WHERE account_key = 'agent-claude-opus' AND status = 'decided'
 GROUP BY 1 ORDER BY 1 DESC;
```
