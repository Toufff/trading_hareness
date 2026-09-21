# 盘中飞书建议运行时

该运行时服务当前可信持仓、正式推荐池以及市场环境，属于研究通知，不连接下单路径。市场环境独立观察沪深核心宽基指数与东财行业板块走势，不把指数或板块混入个股纪律线。

## 固定节奏

| 环节 | 节奏 | 是否调用模型 | 推送规则 |
|---|---:|---|---|
| 本地状态机 | 1 秒 | 否 | 不直接推送 |
| Longhu 盘口取样 | 5 秒 | 否 | 仅交易时段；只在出现新样本后计算 |
| 沪深核心指数 | 15 秒 | 否 | 上证、深证、创业板、科创50、沪深300、中证500、中证1000；达到日内或1分钟异动阈值时合并推送 |
| 行业板块走势 | 1 分钟数据到达 | 否 | 只取行业板块，要求相邻快照方向确认；价格异动为主、同源净流为辅助，不推送普通轮动 |
| 价格/成交额/内外盘异常 | 新样本触发 | 否 | 立即推送确定性证据卡片 |
| DeepSeek | 每个 10 分钟槽位中的前两次 | 是 | 每次落库；默认静默，只有风险升级、关键条件变化或新纪律事件才推送 |
| Codex | 每第三个 10 分钟槽位（约 30 分钟） | 是 | 固定推送；结构固定为大盘、持仓、推荐池、风险；11:35、14:45 替换相邻固定时点 |
| 突发或纪律线补充 | 30–60 秒合并窗口 | Codex | 确定性消息先发，模型失败不阻断前者 |

成交方向使用外盘/内盘增量：外盘增量占优表示主动买盘相对较强，内盘增量占优表示主动卖盘相对较强；它不代表机构或“主力”资金流向。普通事件按标的、类型、方向做 10 分钟冷却；严重度升级可穿透冷却。所有模型输入输出、确定性事件和发送回执均可回放。

飞书使用原生交互卡片：高风险红色、需要关注橙色、常规汇报蓝色。异动卡只保留信号、证据、应对；模型卡按大盘、持仓、推荐池、风险分区。模型内部字段和运行变量不得进入用户可见正文。

## 飞书配置

首选群自定义机器人：创建专用群，在群设置中添加“自定义机器人”，开启签名校验，复制 Webhook 和签名密钥。凭据只写入 `G:\StockPlatform\config\runtime.env`。

```powershell
pwsh G:\StockPlatform\current\scripts\windows\configure-feishu-alerting.ps1 `
  -Transport CustomBot `
  -PromptSigningSecret
```

脚本会隐藏输入 Webhook 和签名密钥，避免凭据进入 PowerShell 历史。

配置并重启后进行真实发送：

```powershell
pwsh G:\StockPlatform\current\scripts\windows\test-feishu-alerting.ps1 -Mode Config
pwsh G:\StockPlatform\current\scripts\windows\test-feishu-alerting.ps1 -Mode Live -SendTest
```

状态接口：`GET /api/v1/intraday/advisory/status`。它不返回 Webhook、密钥或模型凭据。

模型真实连通性（使用合成标的，不发送持仓）：

```powershell
pwsh G:\StockPlatform\current\scripts\windows\test-intraday-advisory-model.ps1 -Provider DeepSeek
pwsh G:\StockPlatform\current\scripts\windows\test-intraday-advisory-model.ps1 -Provider Codex
```

企业应用方式可选：需要 App ID、App Secret、接收群 `chat_id`，并给应用开通 `im:message:send_as_bot`、发布应用、把机器人加入群；配置命令改用 `-Transport App`。

## 失败边界

- 持仓快照过期时只阻断持仓侧，正式推荐池仍可观察。
- 上游时间戳超过 20 秒的报价拒绝参与判断。
- 指数时间戳超过 90 秒的旧收盘数据拒绝参与判断；开盘验收要求至少 4 个核心指数在 30 秒内更新。
- DeepSeek/Codex 失败会留下失败记录，不撤销或延迟已生成的确定性告警。
- 未配置飞书时任务不启动，不空耗模型额度。
- 所有子进程使用隐藏窗口；Codex 只读沙箱，移除 API Key 环境变量，使用现有订阅登录。

## 开盘双阶段验收

生产发布会注册隐藏任务 `trading-hareness-intraday-opening-guard`：

- 09:25 检查 SSE 交易日历、API/数据库、后台租约、飞书配置、纪律与盘中建议循环、非空监控范围；
- 09:32 额外要求 20 秒内取得真实且新鲜的盘中行情。行情成功证据跨 1 秒空闲 tick 保留，避免抽样恰好落在两次 5 秒采集之间而误报；
- 首检失败只重启一次 `trading-hareness-dashboard-runtime`，等待 20 秒后复检，不循环重启；
- 最终结果通过真实飞书卡片发送。绿色表示当日链路已验收，红色明确列出失败项；休市日静默跳过；
- 所有运行都追加无凭据的 JSONL 到 `G:\StockPlatform\logs\runtime\intraday-opening-guard.jsonl`。

该任务只验证研究与提醒链路，不连接券商、不下单。
