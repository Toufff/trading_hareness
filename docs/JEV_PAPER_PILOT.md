# JEV 结构化决策模拟盘试验

2026-09-21。官方技能通过 `npx skills add typesafe-ai/skills --skill typesafe-ai --agent codex --global --yes --copy` 安装，位于 `C:/Users/brave/.agents/skills/typesafe-ai/SKILL.md`。后续修改本接入应先读该技能和最新官方文档。

## 接入形态

JEV 不是聊天模型。官方 HTTP 接口为 `POST https://api.typesafe.ai/v1/systemone`，Bearer 鉴权，输入 `state/model/questions`，输出 `answers/usage/model`。采用版本 `jev-1.13.0`，不随 latest 静默漂移。Choice 选择离散动作；未来可以独立增加 Score/Noul 维度，但其概率不能冒充盈利概率。

实验账户：`agent-jev-pilot`，使用同一套模拟账本和撮合引擎。模型运输和动作适配在 `app/agent_paper/jev.py`，由原有 `agent-paper-trader.py --backend jev` 进入。不修改其他模型、真实持仓、推荐池或纪律线。

当前策略 `jev-choice-pilot-v1` 是技术试验：程序从已有完整行情的标的生成“等待、用现金25%/50%/100%买入、卖出可卖部分25%/50%/100%”离散选项，按100股、T+1、手续费和买卖一价计算数量及限价。每轮最多选择一笔；无完整报价、报价超过120秒、该标的已有挂单、非允许决策时段不提供新增买卖选项。报价可比采集开始时间晚至多10秒，以容纳批量HTTP采集延迟；超过该界限仍拒绝。没有自动仓位集中度减仓规则。

这些限制意味着它与能够自主扩展标的、撰写论证和多笔调仓的 CLI 模拟盘**不是纯模型对照实验**。先验证适配链路，再评估是否扩展多动作、研究目标选择和证据维度；不得把试验盈利归因于模型而忽略不同策略空间。

系统完整保留请求、候选动作、选择概率、版本与调用耗时。中文说明由程序生成，明确标注“程序解释”；不伪造模型思考过程。未加未经验证的置信度交易阈值。

## 本机凭据与操作

用户明确授权的明文凭据只在本机 Vault `私密凭据/JEV API凭据.md`，已被 Git 忽略。外部配置 `G:/StockPlatform/config/jev-paper.env` 只保存该文件引用、显式代理与版本。也支持进程变量 `TYPESAFE_API_KEY`，优先于文件引用。不在日志回显凭据。

从开发仓库运行（模拟盘当前本来就独立从开发 checkout 运行，不需要重启网站）：

```powershell
.\.venv\Scripts\python.exe scripts/agent-paper-trader.py model-check --backend jev --account-key agent-jev-pilot --provider-env-file G:/StockPlatform/config/jev-paper.env
.\.venv\Scripts\python.exe scripts/agent-paper-trader.py init --backend jev --account-key agent-jev-pilot --provider-env-file G:/StockPlatform/config/jev-paper.env
.\.venv\Scripts\python.exe scripts/jev-paper-preview.py
.\.venv\Scripts\python.exe scripts/agent-paper-trader.py report --account-key agent-jev-pilot
```

`init` 只执行一次，已有账户不能重置；起始资金与持仓沿用初始化时最新核验的券商快照并记录来源。
`preview` 从已留存决策上下文进行一次历史复算，结果在 `G:/StockPlatform/reports/jev-pilot/`，绝不写订单或伪造过去收益。
`model-check` 真实调用官方 API，但不会证明投资有效。

初次试验没有安装定时任务。2026-09-23 增加了 Windows 任务的 JEV 后端和独立 provider 配置透传；用户要求从下一个交易日自动运行现有纯 JEV 账户，不创建混合账户、不重置本金或持仓。发布并验收后，任务名为 `trading-hareness-agent-paper-trader-jev`，使用 `G:/StockPlatform/current` 下的发布代码和现有 `agent-jev-pilot` 账本，不依赖开发 checkout。安装命令：

```powershell
pwsh G:/StockPlatform/current/scripts/windows/install-agent-paper-trader-task.ps1 `
  -RepositoryRoot G:/StockPlatform/current `
  -TaskName trading-hareness-agent-paper-trader-jev `
  -AccountKey agent-jev-pilot -Backend jev -Model jev-1.13.0 `
  -ProviderEnvFile G:/StockPlatform/config/jev-paper.env
```

任务每天 09:20 启动，崩溃后每 10 分钟尝试拉起；进程内由已核验的交易日历跳过休市日，在 09:30–11:27 和 13:00–14:56 的决策窗口按 15 分钟间隔调用。进程和账户锁防止重叠，15:01 收尾。任务是纯模拟撮合，不触碰券商。**任务注册、凭据 `model-check` 和明日交易时段真实首轮决策是三个不同的验收层级；收盘后不能声称明日首轮已通过。** 运行证据在 `G:/StockPlatform/logs/agent-paper-agent-jev-pilot.jsonl`，账本和决策从 `/api/v1/agent-paper/status?account_key=agent-jev-pilot` 读回。

手动运行仍可用 `run-day --backend jev --account-key agent-jev-pilot --provider-env-file G:/StockPlatform/config/jev-paper.env --decision-minutes 15`，仅在排障时使用，不与定时任务并跑。

## 验收边界

- 官方 `/v1/models` 与三种原语真实请求成功；JEV版本1.13.0。
- 过期行情、T+1、现金费用、未知选项、坏概率、认证失败以及原始调用留证由独立测试验证。
- 账户初始化、API读回、历史预览的实际结果写在下方验收记录。
- 还没有多日收益或样本外证据，官方功能调用示例也不是盈利策略验证。

### 2026-09-21 本机验收记录

- `model-check` 真实调用成功，约1200ms；只有等待选项，无委托。
- 回放真实留存的 `agent-codex-sol` 14:49:48 上下文，输入27030 tokens，1741ms，四个可选动作，选择等待（置信度0.91）。由于可用现金、报价与可卖量限定选项，不能据此推断对全部股票都建议等待。
- 回放文件：`G:/StockPlatform/reports/jev-pilot/20260921T140747Z-preview.json`；`orders_executed=false`，不写模拟订单或追溯收益。
- 22:11 初始化 `agent-jev-pilot`，采用18:02:37核验快照；初始资产103303.98元、现金648.98元、五只持仓。报告命令及运行中 `5681/api/v1/agent-paper/status?account_key=agent-jev-pilot` 均读回一致，零订单、零决策。
- 后端测试采用本机 venv，从 `quant-service` 目录运行全套 unittest：2869项，OK，跳过109项，其中本次新增9项适配器测试。完整日志 `G:/StockPlatform/reports/jev-pilot/backend-tests.log`。Docker compose 路径因开发 checkout 没有 `QUANT_WRITE_API_KEY` 配置而未运行，未绕过认证；本机测试是当前原生 Windows 部署对应的验证环境。
- 前端 `npm run typecheck`、`npm run build` 通过；仍有既有图形依赖大包提示，无前端代码改动。
- 未发布新的生产 release、未增加自动任务、未改其他模拟账户。账户共用正式数据库，但新适配器手动从开发 checkout 执行。

### 2026-09-21 盘中就绪试运行

**结论：历史上下文真实调用与回滚账本验收通过；尚未进行开盘后的实时持续运行。没有新增定时任务。**

本次新增可复用验收入口，仅允许非交易时段执行：

```powershell
.\.venv\Scripts\python.exe scripts/verify-jev-paper.py --day 2026-09-21 --all-stored
```

验收会真实调用 JEV，从已有模拟盘留存上下文取证；模型调用在事务外，随后将其真实返回送入原有 Runner、撮合、报告读模型，在回滚事务内落库并核对。买卖强制样例单独标记，不冒充模型判断。真实账户及正式模拟账户均不更改。

发现并处理的问题：

1. 最大上下文作为嵌套对象发送触发 HTTP400 `max_tokens_exceeded`，原先只记HTTP状态无法定位。改为官方支持的紧凑UTF-8 JSON文本，字段无删减，最大实测输入31102 tokens；错误日志保留供应商错误类别。不承诺任意增长的输入都可用，超过预算仍明确失败。
2. Longhu早盘毫秒时间 `93003000` 被直接截前六位成为无效的 `93:00:30`。改为先补齐前导零，再去毫秒；无效时间仍拒绝。当天6份历史上下文已存的是错误时间，未伪造修复原始历史，其可选动作仍只有等待；新解析路径用原始时间格式经模拟买卖全链验证。
3. 采集开始时刻与报价回包相差3秒，被当作未来报价误拒绝。仅容许10秒采集延迟，120秒过期限制不变；边界测试覆盖有效、过期和过远未来数据。

实测证据：

| 验收项 | 结果 |
| --- | --- |
| 当日48份真实历史上下文 | 48/48 官方JEV调用、决策及原始记录落库读回通过 |
| 模型调用耗时 | 1358–4980ms；最大输入31102 tokens |
| 当前行情读取 | 36只股票、4个指数、持仓5/5；都是收盘报价，不是盘中新鲜性验收 |
| 强制模拟买卖 | 买入200股，资金扣减及T+1正确；卖出200股，资金增加及费用正确；不是模型选出的交易 |
| 实际run_day循环 | 尾盘单次决策、间隔内不重复调用、收盘收尾及净值记录通过；时间和行情为冻结回放 |
| 隔离 | 所有试验账本回滚后查无遗留；正式JEV账户验收前后完全一致 |
| 专项测试 | 模拟盘41项，其中1项原有可选集成测试跳过；供应商23项通过；另已运行上述真实回滚集成验收 |
| 全套后端/前端 | 2872项，跳过109项，其余通过；typecheck/build通过 |

原始验收：`G:/StockPlatform/reports/jev-pilot/20260921T222922-readiness.json`。完整日志 `full-day-readiness.log` 与 `backend-readiness-tests.log` 同目录。新增入口和修复直接用于开发checkout运行的JEV模拟盘；没有重启或原地修改网站的不可变生产release。

参考：[官方技能](https://github.com/typesafe-ai/skills/tree/main/skills/typesafe-ai)、[HTTP API](https://docs.typesafe.ai/api)、[Choice](https://docs.typesafe.ai/primitives/choice)、[Function calling](https://docs.typesafe.ai/cookbooks/function_calling)、[模型及边界](https://docs.typesafe.ai/models)。
