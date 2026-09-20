# 飞书触线提醒：开通与运行手册

## 结论与边界

- **最快方案（推荐）**：在专用飞书群添加“自定义机器人”，复制 webhook，建议开启签名校验。它只能向这个群推送，但无需申请 App ID 或等待应用审核。
- **完整方案**：创建企业自建应用、启用机器人能力，通过应用身份向指定群或用户发送；适合以后多目标、按用户通知。
- 行情、持仓/推荐范围和纪律线由确定性 watcher 每 30 秒计算。当前 watcher 固定绑定 `citics-primary`；Codex 负责健康巡视、失败诊断和解释，不用大模型逐分钟猜测价格，也不提交订单。
- 当前唯一 writer 是本机 StockPlatform。凭据只落在 `G:\StockPlatform\config\runtime.env`；脚本和状态输出都不打印 webhook、App Secret、签名密钥或接收 ID。`lightServer1` 目前只是反向隧道入口，不存在独立 `intraday_edge` 服务，配置命令不会默认 SSH 或重启远端。

官方契约：

- [飞书群自定义机器人使用指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)
- [自建应用获取 tenant_access_token](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal)
- [发送消息 API](https://open.feishu.cn/document/server-docs/im-v1/message/create)
- [获取群信息 API](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/chat/get)

## A. 自定义机器人（推荐先用）

1. 在飞书新建一个专用群，例如“StockPlatform 提醒”。
2. 群设置 → 机器人 → 添加机器人 → 自定义机器人。
3. 安全设置建议选择“签名校验”，复制 webhook URL 和签名密钥。不要先开 IP 白名单；edge 出口 IP 尚未固定时会导致所有消息被拒。若选关键词校验，必须确保关键词存在于每条通知，当前不推荐。
4. 在仓库根目录执行一条配置命令。签名密钥通过隐藏输入读取，不出现在命令行：

```powershell
pwsh .\scripts\windows\configure-feishu-alerting.ps1 `
  -Transport CustomBot `
  -WebhookUrl 'https://open.feishu.cn/open-apis/bot/v2/hook/你的值' `
  -PromptSigningSecret
```

如果你明确不启用签名，去掉 `-PromptSigningSecret`。配置脚本会写入本机私有环境，同时设置 `QUANT_DISCIPLINE_ALERTS_ENABLED=true`、30 秒检查间隔和 `citics-primary` 账户，然后通过 `G:\StockPlatform\current` 已发布版本的生命周期脚本重启本机 quant-api/watcher；它会拒绝从 `F:` 的开发工作树启动生产。

5. 发一条明确标注“测试消息”的真实验收通知：

```powershell
pwsh .\scripts\windows\test-feishu-alerting.ps1 -Mode Live -SendTest
```

群里出现“飞书告警通道验收成功”才证明网络、签名和群目标全部可用。

## B. 企业自建应用机器人

1. 在[飞书开放平台](https://open.feishu.cn/app)创建企业自建应用。
2. 添加“机器人”能力。
3. 权限管理至少开通 `im:message:send_as_bot`（官方发送接口也接受 `im:message` 或历史 `im:message:send` 三者之一）。本项目的只读群预检还需要 `im:chat:readonly`；如果不需要只读群预检，可以只以真实测试消息验收。
4. 创建并发布版本，使机器人能力、权限和可用范围生效。若发给用户，该用户必须在机器人可用范围内；若发给群，先把机器人加入目标群并确认它可以发言。
5. 准备 `App ID`、`App Secret`、接收 ID 和 ID 类型。群通知推荐 `chat_id`（通常以 `oc_` 开头）；官方接口还支持 `open_id`、`union_id`、`user_id`、`email`。
6. 执行一条命令；脚本会隐藏提示输入 App Secret：

```powershell
pwsh .\scripts\windows\configure-feishu-alerting.ps1 `
  -Transport App `
  -AppId 'cli_你的值' `
  -ReceiveId 'oc_目标群ID' `
  -ReceiveIdType chat_id
```

7. 先做不发消息的 token/目标可见性预检，再显式发一条测试消息：

```powershell
pwsh .\scripts\windows\test-feishu-alerting.ps1 -Mode Live
pwsh .\scripts\windows\test-feishu-alerting.ps1 -Mode Live -SendTest
```

官方接口要求：应用必须开启并发布机器人能力；群目标必须已加入机器人且允许发言；用户目标必须在可用范围内。`tenant_access_token` 最长有效 2 小时，运行时代码会缓存并在到期前刷新，不需要人工维护。

## C. 不需要任何真实凭据的本地验收

以下命令同时跑自定义机器人签名/请求合同和应用机器人的 token/发送合同；HTTP 完全由 mock transport 接管，不访问飞书，也不触碰 `G:` 配置：

```powershell
pwsh .\scripts\windows\test-feishu-alerting.ps1 -Mode Mock
```

配置完成后可只检查字段完整性；输出仅包含布尔状态和模式，不包含任何值：

```powershell
pwsh .\scripts\windows\test-feishu-alerting.ps1 -Mode Config
```

## D. 停用、轮换与故障定位

一条命令停用本机 writer，保留密钥字段以便之后人工决定是否轮换：

```powershell
pwsh .\scripts\windows\disable-feishu-alerting.ps1
```

停用会同时写入 `QUANT_DISCIPLINE_ALERTS_ENABLED=false` 并重启本机运行时，避免“停止推送但 watcher 仍空转”。

密钥轮换时重新运行对应的 `configure-feishu-alerting.ps1` 即可；写入是原子替换，服务只在远端配置成功后重启。

只有未来确实部署并验收了 `/opt/quant-intraday-edge` 与 `quant-intraday-edge.service`，才在配置、停用或测试命令上显式增加 `-ApplyRemoteEdge` / `-RemoteEdge`。当前不要使用这些开关。

常见应用机器人错误：

| 错误 | 含义 | 处理 |
| --- | --- | --- |
| `230002` | 机器人不在群中 | 把已发布应用机器人加入目标群 |
| `230006` | 未启用机器人能力 | 开启能力并重新发布版本 |
| `230013` | 用户/单聊目标不在可用范围 | 扩大应用可用范围并发布 |
| `230027` | 缺权限或授权 | 检查消息权限、发布版本和租户授权 |
| `230034` | ID 或 ID 类型不匹配 | 核对 `receive_id` 与 `receive_id_type` |

自定义机器人失败时优先检查签名密钥是否对应当前 webhook、时间是否偏差超过一小时、机器人是否仍在群内、群安全设置是否后来增加了关键词或 IP 白名单。

## E. Codex 的监控职责

Codex 监控只需要读取：edge `/health`、最近扫描时间、行情证据新鲜度、待投递 outbox、连续投递失败数和最近一次真实验收回执。它可以在确定性服务停止或数据过期时提醒/诊断，但不得自行把“看起来像触线”解释成触线，也不得以同一条已经失效的飞书通道给自身故障告警。真正触线仍由可复现规则、数据库状态转换和投递回执决定。
