# 消息研究实施与验收合同

2026-09-14。行情继续使用 Longhu <=300 封装；消息是独立研究输入，
不是基本面否决器，也不依赖持仓。开发在 F 盘，正式数据在 G 盘 PostgreSQL。

## 结构

`app/event_research/` 分成 source（采集）、contracts（规范/时间）、
repository（追加式证据）、analysis（研究/验证）、pipeline（编排）、
report（同轮输出）。close 和 intraday 消费同一合同，前端读持久化结果。
真实 Longhu PCNewsFlash GetList 获取全市场快讯，不用股票候选反向限定抓取范围。
原文、供应商ID、发布时间、首次获取时间、正文哈希和每轮来源状态分别保存。
来源失败、空列表、历史覆盖不足、分析失败不能混成“无重要消息”。

## 研究边界

- 精确股票实体匹配是线索，不自动证明受益。正文同文去重；更新保留新版本。
- 类别包括宏观/海外、政策监管、产业供需、公司、市场情绪；关键词只路由。
- 重要事件研究包含事实、预期依据、差异、传导、时间、反证和条件；
  引证必须指向本轮实际证据。没有预期证据时 unknown，不能虚构 priced-in。
- API 模型采用环境配置 EVENT_RESEARCH_API_BASE / API_KEY / MODEL；
  有限输入、超时、输出校验；新闻当作不可信材料，无工具、无下单权限。
- 未配置模型仍发布真实快讯/实体线索及准确状态，不能称语义研究完成。
- 人工/当前对话实际核查通过带证据ID的审阅JSON写入。只有审核过的实体与
  业务关联可进入研究观察；不改变正式策略分数/阈值，不自动进入推荐组。
- 历史重跑只读取当时已可用的证据及审阅；当前获取不得回填成历史已知。

## 验收

单测：去重、未来日期、旧闻、正面标题负面预期差、引用伪造、无消息/失败区别、
候选独立于持仓、模型错误隔离、分页<=300。真实验收：来源→PG→研究→九份报告+
总报告→owner→adapter→浏览器；逐层记录，不把单测替代业务验收。
因子收益验证须事前冻结输入、样本外、T+1/费用/涨跌停；未通过前不调权重。

参考：TradingAgents（并行研究）、OpenBB economic calendar（实际/预期/前值）、
FinGPT（实体/关系）、Microsoft RD-Agent（假设/实验/验证），不是收益保证。

## 2026-09-14 真实验收记录

- 正式版本：`20260914T210048-07107d68cc67-dirty`；公网静态版本 `20260914125806`。
- 生产程序真实采集4批，每批300条；数据库有1205条去重证据，路由711条。
  窗口有限，不是全市场全历史覆盖；60条是预备模型输入，不是已研究60条。
- 当前对话实际审阅9条证据，写入5组事件，涵盖AI安全/算力分化、利率预期、
  油价/运输、产业政策意向、个股停牌收购。模式 `explicit_evidence_review`，
  不是后台模型成功、也不是一手公告全部核验。
- 消息轮次 `d8182d99-7f60-411d-8be8-d03e53cb2130`；收盘轮次
  `6dabc48c-04a0-46e4-9fcc-3a72602c40cb`。九份独立报告和总报告使用同一消息快照。
- 全量pytest 1953通过/80跳过；前端85通过、类型检查与构建通过。
  真实HTTP/数据库/报告11项消息检查通过；报告发布16项检查通过。
  公网无头Edge桌面1440与手机390宽实测，10个报告页签可读，截图已人工查看。
- 真实验收曾抓到总报告消息先于结果的回归；已修正并加入测试，未放宽验收条件。
- 可复验：`scripts/verify-event-research-live.py --date 2026-09-14`；
  加 `--run-scan` 会实际重跑并发布当日报告，只应在明确授权时使用。
  回执 `G:/StockPlatform/reports/events/live-acceptance.json`；
  浏览器测试 `frontend/e2e/event-research-live.spec.ts`（RUN_EVENT_RESEARCH_LIVE=1）。

### 仍未完成，不得对外称“整套全自动已完成”

1. runtime.env尚无后台模型三项配置，已询问用户使用哪个现有服务；不能擅用无关凭据。
   定时采集与线索可以执行，但新的语义结论不会凭空生成。本次审阅不能代替明天的模型调用。
2. 语义引用校验不等于独立事实复核；未实现事件因子收益的独立样本外验收，不自动调权。
3. 收盘真实验收通过；盘中集成经过回归，本次没有在真实交易时段重跑盘中采集验收。
4. 发布期间共享给朋友的兼容API探针仍返回422（本机消息接口正常）；本次未修该独立问题。
   原有市场指数日期缺口也不属于此次已解决范围。

## 2026-09-14 用户指定 DeepSeek 官方 API

后续配置以此节为准，上一节“尚无配置”是该次验收时的历史状态。
用户指定 DeepSeek-V4.1-Flash；官方实际模型 ID 为 `deepseek-flash`，
`https://api.deepseek.com`。已通过该账户 `/models` 和小型JSON生成请求验证。
配置只写在 `G:/StockPlatform/config/runtime.env`：

```dotenv
EVENT_RESEARCH_API_BASE=https://api.deepseek.com
EVENT_RESEARCH_MODEL=deepseek-flash
EVENT_RESEARCH_API_KEY_FILE=C:/Users/brave/.dsh/.credentials.yaml
EVENT_RESEARCH_API_KEY_REF=DEEPSEEK_API_KEY
EVENT_RESEARCH_THINKING=disabled
```

model_config读取显式配置的外部YAML `refs`，不改动DSH，不复制密钥。
`EVENT_RESEARCH_API_KEY`仍支持环境直接传入（优先），但本机不设置它。
调用使用流式SSE、20秒空闲超时和240秒总截止检查；总截止检查发生在读取到帧时，
阻塞读最多还受20秒空闲超时限制。长度截断、空输出和未正常结束均判失败，不保存为成功。
最多6个重点事件，每字段控制篇幅；最大8192输出token。使用量随分析回执记录。
首次完整非流式请求超时，后改为流式；不能拿小型鉴权探针代替完整语义研究验收。
完整实际结果查看 `event_research_runs.result.analysis` 的 mode/model/usage，
只有 `mode=model_api` 且 completed 才证明模型分析完成；cache_hit需另注明。

官方模型说明：https://deepseek.com/news/deepseek-v4-1-flash/

完整模型实测已通过：`bbbe1671-4096-450f-8e70-360f20312ace`，60条输入，
返回 `deepseek-flash`，mode=model_api/status=completed；输入13102/输出2191 token。
原始引用校验仍严格；当模型没有预期依据却标正/负预期差时，仅保守降为unknown，
记录uncertainty_adjustments，不补造证据；本次成功调用未触发该修正。
# Scheduled visible delivery (2026-09-15)

## Citation transport recovery — 2026-09-15

Model inputs now carry request-local `D001` references instead of canonical
64-character evidence hashes. Exact aliases map back to original immutable
document IDs before the unchanged evidence/content validator and persistence.
No fuzzy ID matching, missing-reference deletion or unverified success.
For invalid aliases only, one bounded model call rechecks the affected events
against the same documents, returning only citation corrections. Good events
and all reasoning remain unchanged. Repairs share the original 240-second
deadline and are rejected if incomplete, unsupported or out of scope.

Analysis receipts retain citation scheme/map hash/input document IDs, repair
count, per-call usage and total tokens. Pre-change model cache entries are not
treated as acceptance of the new contract. Human explicit reviews keep their
original canonical-evidence contract.
Failures distinguish connection/HTTP, generation timeout, JSON parsing,
citation validation/repair and content validation. UI/report show the stage;
safe validation reason and completed-attempt usage remain in the DB receipt.
No credentials, raw response bodies or private reasoning enter diagnostics.

Real 120-document acceptance uses `scripts/test-event-model-large.py`.
`--inject-bad-citation` explicitly corrupts one response reference in the isolated
test to exercise a real second model call; it never publishes or marks a
scheduled slot successful. Ordinary and fault-injected evidence are separate.
Passing citation existence is not proof of factual entailment, company benefit
or investment returns; provider-news provenance remains visible.

The news lane no longer depends on the close scan starting. Windows task
`trading-hareness-event-research-delivery` targets **09:00, 12:00, 22:00 Shanghai
time on exchange trading days**, NOT civil workdays. For every market closure:
**last trading day 22:00, calendar eve of the next trading day 22:00, next trading
day 09:00**. Endpoints are counted once. No scheduled acquisition/LLM in the
middle of the closure. For an ordinary weekend this is Friday 22, Sunday 22,
Monday 09; the old Sunday-noon rule is superseded.

The reviewed `app/event_research/calendar_2026.json` contains SSE annual-notice
holiday ranges and source/date provenance; weekends remain closed even on
government make-up workdays. This immutable release configuration, shared by
scheduler/freshness/scan guards, avoids inferring closure from missing DB bars.
Unreviewed years do NOT fall back to weekdays: task failure is recorded, UI
shows unknown next target, and expiry warning begins 30 days before year end.
Add and verify the next annual notice before expiry. Emergency exchange
closure announcements require updating the reviewed calendar and release.
The known calendar currently covers 2026 only, not an evergreen promise.

Windows registers three daily silent *candidate* times (08:55/11:55/21:55);
the Python calendar gate returns before acquisition/model calls on non-slots.
This supports long holidays without dozens of fragile one-off OS triggers.
2026 examples: Mid-Autumn Sep24 22 → Sep27 22 → Sep28 09; National Day
Sep30 22 → Oct07 22 → Oct08 09; Spring Festival Feb13 22 → Feb23 22 → Feb24 09.

The silent GUI task host starts five minutes before the target. Failed slots
retry every ten minutes through target +35 minutes, with an eight-minute
process cap. Successful slots deduplicate through PostgreSQL automation
receipts. Two real 60-document reviews exceeded the old 120-second model cap;
the cap is now 240 seconds (still bounded), recording elapsed time and safe
character/chunk progress on timeout without saving unfinished reasoning.
No candidate/evidence coverage was reduced to make a test pass. A DB advisory
lock excludes manual/scheduled overlap. A powered-off
PC, logged-out Interactive task principal, vendor outage or model delay may
miss a target: the UI shows actual cutoff/publication time, next target and
overdue state, not a promised future success. It does not wake or focus a
broker window. Explicit on-demand refreshes remain available. Between the last
session close and the next session open, scan-triggered news refresh is disabled:
scans consume the scheduled news snapshot without creating extra weekend runs.
Trading-day scans may still request extra updates beyond the three minimum slots.

Each delivery must freshly collect Longhu news (physical batches <=300),
complete citation-validated DeepSeek interpretation, write a standalone
Markdown/JSON report, and verify the same run/hash through the owner HTTP API.
Only then does the durable receipt become completed. News interpretations
include transmission, observation handling, counterevidence and invalidation;
raw headlines alone are not an applied update. This remains research, not an
automatic trade or strategy-weight promotion.

Market and strategy views poll the latest news every 60 seconds while visible.
Strategy views apply macro and matching-stock event observations as a distinct
current-news overlay. Original report rankings, price cutoffs and historical
news snapshots remain frozen; downloading that report does not backdate new
facts. Freshness follows delivery slots, not the old one-hour expiry.

Manual production acceptance uses the same entry point:
`scripts/windows/run-event-research-delivery.ps1 -Manual`.
It records a separate manual key and never completes a future scheduled slot.
Evidence: `G:/StockPlatform/logs/event-research-delivery.jsonl`,
`quant.automation_runs` task key `event_research_delivery`, and
`G:/StockPlatform/reports/events/<event_run_id>.{json,md}`.

Large model acceptance: `scripts/test-event-model-large.py --documents 120
--deadline 540 --output G:/StockPlatform/reports/events/<receipt>.json` collects
fresh source batches, chooses 120 real category-balanced documents, bypasses
the analysis cache, calls the configured official model and validates citations
and complete reasoning. It records tokens, actual elapsed time and whether it
fits the ordinary 240-second budget. Its generous diagnostic deadline does not
change production's SLA or count as a successful scheduled delivery. No key is
written to the receipt. Do not replace this with a tiny connectivity probe.
