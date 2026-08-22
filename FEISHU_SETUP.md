# 飞书群消息汇集：上线与验收

这份清单只包含可复核的配置项；不会记录 App Secret、OAuth code、access token 或 refresh token。若这些凭据曾出现在聊天记录或终端输出中，请先在飞书开放平台轮换应用密钥，并重新授权用户 OAuth。

## 已实现的运行模型

- 每 10 秒以 **用户 OAuth** 轮询已登记的外部源群；机器人无需加入源群，但授权用户必须能查看该群。
- 每个源群始终发送到主汇总群，也可登记至多 8 个额外目标群。额外群必须由你在飞书中创建并先邀请机器人；例如只在 `#liwei` 路由填写新建“liwei 消息转发群”的 `chat_id`，即可一条源消息同时投递汇总群和该专属群。
- 以 `message_id` 去重，首次发现源群只建立基线；随后新增消息才会被汇总。
- 文本、图片、视频、文件、富文本分别走飞书支持的消息形态。图片/视频和来源标签在同一个富文本气泡中；普通文件使用单一文件气泡，并把来源标签写入文件名。
- 源消息编辑会更新机器人原先发送的同一条文本或富文本消息；撤回会撤回该副本。行动卡片默认关闭，保证“一个源消息 = 一个汇总气泡”。
- Vue 工作台使用 Element Plus 和 ECharts：查看轮询健康、最近更新时间、消息记录，并可新增、编辑、停用或删除源群及标签。

## 飞书开放平台必须完成的配置

1. 在“权限管理”申请并发布下列最小权限。权限申请后需重新发布应用；用户身份权限变动后还需要让用户重新 OAuth 授权。

   | 用途 | 权限 / 产品 |
   | --- | --- |
   | 读取外部源群文本/消息元数据 | 用户 OAuth：`auth:user.id:read`、`im:chat:readonly`、`im:message`、`im:message.group_msg`、`im:message.group_msg:get_as_user`、`offline_access` |
   | 转发源群图片、文件、音视频 | 用户 OAuth 还须在授权链接中显式请求 `im:message.group_msg:get_as_user` 与 `im:resource`，并由机器人应用上传到汇总群；外部群的资源可见性以接口实测为准 |
   | 汇总群发送与媒体重传 | 机器人消息发送权限，以及 `im:resource`；机器人必须已加入汇总群 |
   | 只读核验应用权限、事件、版本 | `application:application:self_manage`（或管理端的 `admin:app.info:readonly`） |
   | 搜索可访问消息 | 用户 OAuth：`search:message` |
   | 图片 OCR | 应用身份：`optical_char_recognition:image` |
   | 音频转写 | 应用身份：`speech_to_text:speech` |
   | 文本翻译 | 应用身份：`translation:text` |
   | Drive 大文件归档 | 应用身份：`drive:file:upload`，并给应用目标文件夹访问权 |
   | Wiki 归档 | 应用身份：`wiki:wiki`，并把应用设为知识空间成员/管理员或授权目标节点 |
   | Base 台账 | `base:record:create`、`base:record:retrieve`、`base:record:update`、`base:record:delete`，并共享目标 Base |
   | 任务与日历 | `task:task:write`、`calendar:calendar`，并赋予相应清单/日历可见权限 |
   | 审批 | `approval:approval` 和已发布的审批定义 |

2. 若要启用行动卡片，在“事件订阅”使用长连接或有效回调地址，订阅 `card.action.trigger`；如要用表情协作再订阅消息反应事件。设置 `FEISHU_GROUP_RELAY_ACTION_CARDS_ENABLED=true` 后，卡片会成为转发消息之后的**第二个**气泡。

3. 若要启用飞书 H5 工作台与机器人菜单，设置 `FEISHU_WORKBENCH_PUBLIC_BASE_URL` 为已在开放平台登记的公网 HTTPS 域名；不要填 `localhost`。在机器人菜单中将事件 key 配为 `workbench`。

4. 在汇总群和每个额外目标群中确认机器人可发送消息。外部源群无需加机器人，但 OAuth 授权用户必须实际可见每个源群。

## 服务配置

将可选资源 ID 写入部署环境，而不是前端：

```dotenv
FEISHU_GROUP_RELAY_POLL_SECONDS=10
FEISHU_GROUP_RELAY_ACTION_CARDS_ENABLED=false
FEISHU_WORKBENCH_DRIVE_FOLDER_TOKEN=
FEISHU_WORKBENCH_WIKI_SPACE_ID=
FEISHU_WORKBENCH_WIKI_PARENT_NODE_TOKEN=
FEISHU_WORKBENCH_TASKLIST_GUID=
FEISHU_WORKBENCH_BASE_APP_TOKEN=
FEISHU_WORKBENCH_BASE_TABLE_ID=
FEISHU_WORKBENCH_CALENDAR_ID=
FEISHU_WORKBENCH_APPROVAL_CODE=
FEISHU_WORKBENCH_PUBLIC_BASE_URL=
```

源群及路由标签不需要改环境文件：在 `http://localhost:5680/workbench` 的“群消息转发状态”里维护。企业限制“搜群/列群”时，可直接填写已知 `chat_id` 完成注册。

新建 liwei 专属群后，在编辑 `#liwei` 路由时，将该群 `chat_id` 填入“额外转发目标群”。也可以通过环境变量在首次初始化时设置：

```dotenv
FEISHU_GROUP_RELAY_LIWEI_TARGET_CHAT_IDS=oc_your_liwei_forward_group
```

该变量只用于首次初始化默认路由；已经运行过的部署应在工作台保存路由，或调用 `PUT /api/group-relay/routes/liwei` 并传入 `target_chat_ids`。

## 验收顺序

1. 打开 `/api/group-relay/status`：`overall` 应为 `healthy`，`user_oauth_scope_audit.verified` 应为 `true`，三组源群应显示最近一次轮询时间。`delivery_state=awaiting_message` 只代表尚未观察到新的真实消息，不是发送成功的证据。
2. 在每个源群发送一条新文本、图片、视频、文件和富文本，确认汇总群每条只出现一个带 `#标签` 的副本。
3. 重发或等下一轮轮询，确认没有重复副本；编辑一条文本或富文本后，确认原汇总消息被更新而非新增。
4. 在工作台点击“复核后台配置”，或调用 `POST /api/feishu-workbench/application-inspection`。它会独立只读检查机器人能否读取汇总群；同时尝试读取应用当前的权限、事件和发布状态，后者需先申请 `application:application:self_manage`。随后检查 `/api/feishu-workbench/status` 中 OAuth scopes、`event_subscriptions` 和可选能力的 `authorization_status`。用户身份能力会根据已保存的 OAuth scope 显示 `verified` 或 `missing`；应用身份能力显示 `awaiting_verification`，直到飞书后台权限、资源可见性和首次实际调用均通过。事件显示“等待首个回调”只表示本地处理器已注册；需要在飞书中真正触发一次操作，才算平台订阅已验收。
5. 最后再分别试 OCR、音频转写、翻译、Wiki/Base/任务/日历/审批；每一项失败都会直接返回飞书权限或资源可见性错误，不会记录为成功。

## 已知边界

- 飞书富文本可以把标签、图片、视频放进同一气泡；普通文件没有等价的富文本元素，所以以一个原生文件气泡发送，标签在文件名中。
- 直接 IM 媒体限制为单图 10 MiB、单文件 30 MiB。超过 30 MiB 且已配置 Drive 时会流式归档到 Drive；未配置时明确返回 `unsupported`。
- 当前“音频转写”仅将 Opus、MP3、WAV、M4A 送入飞书文件 ASR；该接口适用于不超过 60 秒的音频。视频会正常转发，但要转写需先在独立媒体流水线提取音轨。
