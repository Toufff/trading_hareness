# 盘中飞书建议运行时

该运行时只服务当前可信持仓与正式推荐池，属于研究通知，不连接下单路径。

## 固定节奏

| 环节 | 节奏 | 是否调用模型 | 推送规则 |
|---|---:|---|---|
| 本地状态机 | 1 秒 | 否 | 不直接推送 |
| Longhu 盘口取样 | 5 秒 | 否 | 仅交易时段；只在出现新样本后计算 |
| 价格/成交额/主动侧异常 | 新样本触发 | 否 | 立即推送确定性证据卡片 |
| DeepSeek | 10 分钟 | 是 | 每次落库；风险状态或关注集合变化才推送 |
| Codex | 30 分钟 | 是 | 固定推送；11:35、14:45 替换相邻固定时点 |
| 突发或纪律线补充 | 30–60 秒合并窗口 | Codex | 确定性消息先发，模型失败不阻断前者 |

成交方向使用外盘/内盘增量作为主动侧代理，不声称是机构或“主力”身份。普通事件按标的、类型、方向做 10 分钟冷却；严重度升级可穿透冷却。所有模型输入输出、确定性事件和发送回执均可回放。

飞书使用原生交互卡片：高风险红色、需要关注橙色、常规汇报蓝色；卡片正文突出触发证据、关注标的、条件式建议与风险。

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
- DeepSeek/Codex 失败会留下失败记录，不撤销或延迟已生成的确定性告警。
- 未配置飞书时任务不启动，不空耗模型额度。
- 所有子进程使用隐藏窗口；Codex 只读沙箱，移除 API Key 环境变量，使用现有订阅登录。
