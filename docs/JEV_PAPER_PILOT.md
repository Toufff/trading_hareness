# JEV 结构化决策模拟盘试验

2026-09-21。官方技能通过 `npx skills add typesafe-ai/skills --skill typesafe-ai --agent codex --global --yes --copy` 安装，位于 `C:/Users/brave/.agents/skills/typesafe-ai/SKILL.md`。后续修改本接入应先读该技能和最新官方文档。

## 接入形态

JEV 不是聊天模型。官方 HTTP 接口为 `POST https://api.typesafe.ai/v1/systemone`，Bearer 鉴权，输入 `state/model/questions`，输出 `answers/usage/model`。采用版本 `jev-1.13.0`，不随 latest 静默漂移。Choice 选择离散动作；未来可以独立增加 Score/Noul 维度，但其概率不能冒充盈利概率。

实验账户：`agent-jev-pilot`，使用同一套模拟账本和撮合引擎。模型运输和动作适配在 `app/agent_paper/jev.py`，由原有 `agent-paper-trader.py --backend jev` 进入。不修改其他模型、真实持仓、推荐池或纪律线。

当前策略 `jev-choice-pilot-v1` 是技术试验：程序从已有完整行情的标的生成“等待、用现金25%/50%/100%买入、卖出可卖部分25%/50%/100%”离散选项，按100股、T+1、手续费和买卖一价计算数量及限价。每轮最多选择一笔；无完整报价、报价超过120秒、该标的已有挂单、非允许决策时段不提供新增买卖选项。没有自动仓位集中度减仓规则。

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

初次试验没有安装定时任务。需要手动跑一个真实交易日时使用既有命令 `run-day`，追加 `--backend jev --account-key agent-jev-pilot --provider-env-file G:/StockPlatform/config/jev-paper.env --decision-minutes 15`。它仅调用模拟撮合。当前新入口只在开发 checkout，生产不可变 release 不作原地修改。

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

参考：[官方技能](https://github.com/typesafe-ai/skills/tree/main/skills/typesafe-ai)、[HTTP API](https://docs.typesafe.ai/api)、[Choice](https://docs.typesafe.ai/primitives/choice)、[Function calling](https://docs.typesafe.ai/cookbooks/function_calling)、[模型及边界](https://docs.typesafe.ai/models)。
