# 本地 n8n + 飞书机器人运行手册

最后确认：2026-08-08。本目录是一套仅绑定本机回环地址的飞书机器人自动化服务。它使用 Colima 提供 Docker，不依赖 Docker Desktop。

## 当前工作流

当前有四个生产工作流，均已发布并启用：文本入口 `飞书入口：文本汇聚`（ID：`xo3AHKRr4MFXrzFA`，`/webhook/feishu-market-text`）、媒体分片入口 `媒体原生：单分片上传`（ID：`mediaPartFlow123`，`/webhook/feishu-media-part`）、媒体完成提交 `媒体原生：完成提交`（ID：`mediaFinalize123`，`/webhook/feishu-media-finalize`）和上传会话状态核验 `媒体原生：上传会话状态核验`（ID：`mediaStateFlow123`，`/webhook/feishu-media-state`）。拆分是为了避免大图/音频/视频在一个含二进制的复杂 Code 图中阻塞。

它目前只有第一个入口节点：

```text
飞书群消息
  -> 飞书长连接（feishu-adapter）
  -> POST http://n8n:5678/webhook/feishu-market-text 或 /feishu-media-part
  -> n8n 原生 HTTP Request 分片节点
```

- Webhook：`POST /webhook/feishu-market-import`，节点路径配置为 `feishu-market-import`。
- 不要使用 `/webhook-test/feishu-market-import`；那只用于编辑器中的临时测试监听。
- 适配器请求 n8n 时的 body 为：

```json
{
  "source": "feishu",
  "receivedAt": "接收时刻（ISO 8601）",
  "event": { "飞书 im.message.receive_v1 的原始事件" },
  "message_text": "从文本或富文本提取的正文"
}
```

常用字段位于 `event.message`：`message_id`、`chat_id`、`message_type` 与 JSON 字符串 `content`。文本为 `content.text`；富文本的文本与图片位于 `content.content_v2`。发送者信息位于 `event.sender.sender_id.open_id`。

文本使用 JSON 入口；含图片、音频或视频时，适配器逐分片 POST multipart 到 `feishu-media-part`，n8n 按远端契约 reserve → PUT `/parts/{part_number}` → finalize，最后调用 submit。文本工作流也以 JSON body 调用远端 `items/text`，然后重新 GET batch 确认正文已持久化；表单字段会造成远端文本项停留在 `uploading`，不可使用。多媒体消息按资源逐个完成 finalize，不会只提交最后一个文件。目标服务目前接受 JPEG、PNG、WebP、MP3、M4A、WAV、MP4、MOV。

### 音视频分片发布门槛

远端 `/api/v1/imports/limits` 当前返回 `chunk_bytes=8388608`（8 MiB）、`file_bytes=524288000`（500 MiB）。适配器严格按 8 MiB 生成分片清单，`UPLOAD_PART_BYTES` 只能设为 `8388608`；`PUT /parts/{part_number}` 只发送原始二进制和必需的 `X-Content-SHA256`，不发送 `Content-Range`。n8n 的 `N8N_PAYLOAD_SIZE_MAX` 与 `N8N_FORMDATA_FILE_SIZE_MAX` 已设为 512 MiB，以覆盖单文件上限。reserve 请求保持 OpenAPI 定义的字段，上传状态通过 `GET /uploads/{upload_id}` 查询，最后由 `finalize` 校验完整 SHA256。远端 OpenAPI 位于 `/api/v1/openapi.json`，`/api/docs` 是网页登录入口；API 使用 `reports:read` Bearer Token。

适配器按 `event_id` 和 `message_id` 做 10 分钟幂等去重；飞书重复投递会复用第一次处理结果，不会重复下载或创建目标媒体上传。n8n 执行历史已开启自动清理：保留 72 小时、最多 1000 条；成功执行不保存二进制，失败执行保留用于排错。历史执行产生的旧文件会由 n8n 后台清理周期逐步回收，不要直接删除 Docker volume 或 `storage` 子目录。

每个媒体只在首个分片创建 batch/upload 会话；后续分片复用相同会话。多个媒体会逐个 finalize，并在最后一个媒体完成后只 submit 一次 batch。重试时先通过状态核验读取远端 `received_parts`，只补传缺失分片。大文件按 8 MiB 分片，n8n 不把整文件交给 Code 节点。若需重新生成原始工作流，先备份数据库，再执行导入脚本并重新发布上述工作流。

导入时间默认采用适配器收到飞书事件时的 `Asia/Shanghai` 时间。要覆盖它，请在路由标签后的首行写 `@YYYY-MM-DD HH:mm`；这一行不会写入正文。例如：

```text
#liwei
@2026-07-31 14:30
复盘正文……
```

后续要处理或转发消息时，在 Webhook 节点后添加节点即可，例如 Code 节点解析 `event.message.content`，然后用 HTTP Request 节点向目标接口 POST。工作流和执行历史保存在 PostgreSQL 持久卷中。

## 服务组成与入口

```text
飞书开放平台
  -- 长连接 --> feishu-adapter (容器内 3000)
                    |-- 本机消息监控： http://localhost:5680
                    `-- n8n Webhook： http://n8n:5678/webhook/feishu-market-import
                                      |
                                      `--> n8n： http://localhost:5678
                                               |
                                               `--> PostgreSQL 持久卷
```

所有宿主机端口均绑定 `127.0.0.1`，因此不能由局域网或公网直接访问。

| 服务 | 容器名 | 用途 | 本机地址 |
| --- | --- | --- | --- |
| n8n | `n8n` | 编辑和执行自动化工作流 | http://localhost:5678 |
| 飞书适配器 | `n8n-feishu-adapter` | 官方 SDK 长连接、转发事件、消息监控 | http://localhost:5680 |
| 量化研究 | `n8n-quant-research` | 分析师信号、行情日线、候选池和回测基础数据 | http://localhost:5681 |
| PostgreSQL | `n8n-postgres` | n8n 的工作流、凭据与执行数据 | 无宿主机端口 |

监控页通过 Server-Sent Events 实时显示当前适配器进程收到的最近 200 条事件。它显示文本、消息/群聊/发送者 ID、图片或文件 key、完整原始 JSON，以及“已接收 → n8n 执行中/完成/失败 → 目标导入队列状态”。重启 `feishu-adapter` 后该页面的内存记录会清空，不影响 n8n 的执行历史。

本机 relay/monitor 页面由 `frontend/` 中的 Vue 3 + Vite + TypeScript 构建产物提供。relay 使用 multipart，不再把媒体编码为 Base64；浏览器显示上传进度并支持取消。适配器提供 `/api/config`、`/jobs`、`/metrics`，其中 job/asset/part 状态持久化在 PostgreSQL，临时媒体文件存放在 `adapter_ingestion_data` volume，由本地对账定时清理。

幂等规则：同一 `event_id` 或 `message_id` 永远复用本地 job；已完成过的相同媒体 SHA256 会标记为 `duplicate_media`，不会再次调用远端 reserve。批次、正文和媒体上传键均为确定性 idempotency key；远端 HTTP 409 会分类为 `remote_conflict`，保留 job 和临时分片供人工检查；只有通过 `POST /api/jobs/{job_id}/retry` 明确重试，才会重新进入本地队列。

错误与分析：n8n 的统一错误工作流 `统一错误记录：本地 ingestion` 会把执行错误写入 `ingestion_errors`。本地队列对账现在由 `feishu-adapter` 内部定时器执行，覆盖显式重试队列、分析队列和孤儿临时文件清理；历史 n8n workflow `本地队列：定时对账` 保留为可选手动入口但默认停用，避免制造卡住的 execution 状态。`analysis_jobs` 在本地只记录“已送远端档案”的状态；量化观点唯一由远端 47 的已解析报告同步工作流写入。因而本机不运行 OCR、ASR、视频帧抽取或第二套媒体/文本观点抽取，也不会保存一份本地媒体副本用于量化。量化 API、Tushare 配置和服务器暴露方式见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 本机统一手动投递

当来源飞书群不允许机器人加入，或消息来自个人微信时，使用 [本机消息投递台](http://localhost:5680/relay)，无需再把内容发送到机器人中转群。它仅在本机可访问，支持：

- 粘贴正文，拖入或直接粘贴多张图片/音频/视频；
- 选择 `#liwei`、`#liuzi`、`#xiaolan`、`#anqiang` 或 `#quanneng`；
- 填写来源备注；
- 留空时用收到内容时的北京时间，或显式指定日期和时间。

投递台直接复用市场复盘工作流；一条投递会在目标服务中生成一个含正文和全部媒体的导入批次。SPA 的 multipart 入口单个媒体最大 500 MB，一次最多 12 个媒体；旧 JSON/Base64 兼容入口仍限制为 12 MB，不能用于大媒体。

### 剪贴板快捷键

在任意桌面应用中复制文字后，可运行 [`scripts/relay-from-clipboard.sh`](scripts/relay-from-clipboard.sh)。脚本会把文字放入只保留 5 分钟的一次性本机草稿，并自动打开投递台、填好正文；剪贴板内容不会出现在 URL 或浏览器历史中。图片仍可在投递台内直接粘贴。

已安装的本机 LaunchAgent `com.papa.market-relay-hotkey` 会在登录后自动注册全局快捷键 `⌃⌥⌘R`，不需要再在“快捷指令”中配置。该服务只会在按下快捷键时读取一次剪贴板；不会持续监控、保存或上传剪贴板内容。若“快捷指令”里留有刚才新建的空白快捷指令，可直接关闭且不保存。

### 微信本机图片监控

如果只需要转发 Mac 微信里指定群的本机图片/音频/视频，可以启动本机 watcher。它只读取本机微信媒体临时目录中的新增媒体文件，不读取微信数据库、Cookie、网络流量或聊天文本；启动前已有的历史文件会被忽略。

当前配置只监听“小蓝炒股会”对应的本地目录 ID `71345daa03ac00d81e0f824bb580d85e`，包括该群的 `msg/attach/...` 与 `temp/...` 媒体目录；不再监听无法区分群聊的 `InputTemp`。路由默认按 `#xiaolan` 投递；需要改目标分析师时传入标签：

```bash
n8n/scripts/start-wechat-image-relay.sh xiaolan
n8n/scripts/start-wechat-image-relay.sh liuzi
n8n/scripts/start-wechat-image-relay.sh liwei
```

停止服务：

```bash
n8n/scripts/stop-wechat-image-relay.sh
```

日志与去重状态：

```bash
tail -f n8n/logs/wechat-image-relay.log
cat n8n/state/wechat-image-relay.pid
```

当前机器还安装了用户级 LaunchAgent：

```bash
launchctl print gui/$(id -u)/com.papa.wechat-image-relay
```

如需改成其他群，先通过受控发图实验确认该群目录 ID，再设置：

```bash
WECHAT_RELAY_CHAT_DIR_ID=<目录ID> n8n/scripts/start-wechat-image-relay.sh xiaolan
```

## 配置与敏感信息

私密配置在本目录的 [`.env`](.env) 中，权限应保持为仅当前用户可读。它至少包含：

```dotenv
POSTGRES_PASSWORD=...
N8N_ENCRYPTION_KEY=...
FEISHU_APP_ID=...
FEISHU_APP_SECRET=...
N8N_PORT=5678
# 可选：未设置时市场数据同步保持 disabled
TUSHARE_TOKEN=...
QUANT_UNIVERSE=000300.SH,600519.SH,300750.SZ
```

`.env` 已被 Git 忽略。不得将其内容、飞书 App Secret 或 n8n 加密密钥复制进文档、提交或聊天记录。`N8N_ENCRYPTION_KEY` 不能在已有数据时随意更换，否则先前保存的 n8n 凭据将无法解密。

飞书开放平台需要保持：

- 订阅方式为“使用长连接接收事件/回调”。
- 已启用事件 `im.message.receive_v1`。
- 机器人已加入目标群聊。
- 若要由适配器下载消息图片、音频、视频或文件，申请并发布 `im:message:readonly`（或更高的 `im:message`）权限。未授权时，监控页会明确显示权限缺失；文本转发不受影响。

## 开机和登录后自动启动

已配置两层 LaunchAgent：

1. Homebrew 的 `homebrew.mxcl.colima` 在用户登录后启动 Colima/Docker。
2. `~/Library/LaunchAgents/com.papa.n8n-compose.plist` 在登录后运行本目录的 [`scripts/start-compose.sh`](scripts/start-compose.sh)。脚本最多等待 Docker 120 秒，然后执行 `docker compose up -d`。

这个 Compose 启动脚本是一次性任务，所以在 `launchctl print` 中看到 `state = not running` 且 `last exit code = 0` 是正常的：它表示已成功拉起容器后退出，并非服务故障。Compose 服务本身使用 `restart: unless-stopped`。

## 日常操作

在此目录执行：

```bash
cd /Users/papa/codebase/n8n

# 查看容器状态
docker compose ps

# 日常启动或恢复；保留已有数据
docker compose up -d

# 修改 feishu-adapter 代码或 Compose 后，重建该适配器
docker compose up -d --build feishu-adapter

# 查看飞书连接和转发日志
docker compose logs -f feishu-adapter

# 查看 n8n 日志
docker compose logs -f n8n

# 停止容器（不删除数据；下次可用 up -d 恢复）
docker compose stop
```

不要运行 `docker compose down -v`，它会删除 PostgreSQL/n8n 数据卷，造成工作流、凭据和执行历史丢失。

若 Docker 本身未启动：

```bash
colima start
cd /Users/papa/codebase/n8n && docker compose up -d
```

## 检查与故障排查

```bash
# n8n 与消息监控健康状态
curl -fsS http://127.0.0.1:5678/healthz
curl -fsS http://127.0.0.1:5680/health

# 检查自动启动配置（当前用户）
launchctl print gui/$(id -u)/com.papa.n8n-compose
brew services list | rg '^colima\\s'
```

排查顺序：

1. `docker compose ps` 应显示 `n8n`、`n8n-feishu-adapter` 为 `Up`，`n8n-postgres` 为 `healthy`。
2. 在 http://localhost:5680 打开监控页并发送一条群消息。页面出现消息但 n8n 无执行记录，检查 `N8N_WEBHOOK_URL` 与工作流是否启用。
3. 页面没有消息，查看 `docker compose logs -f feishu-adapter` 是否有 `ws client ready`、`Forwarding im.message.receive_v1`；没有则检查飞书长连接、事件订阅、应用版本发布和机器人是否在群内。
4. n8n 的执行记录可以在编辑器的 Executions 页面查看；也可从适配器日志确认它已将事件 POST 给 n8n。
5. 开机后服务没有出现，先运行 `colima start`，再执行 `docker compose up -d`，并检查 `/Users/papa/Library/Logs/n8n-compose-launchd.log`。

## 关键文件

- [`compose.yaml`](compose.yaml)：容器、网络端口、持久卷与环境变量映射。
- [`feishu-adapter/index.mjs`](feishu-adapter/index.mjs)：飞书官方 Node SDK 长连接、转发到 n8n、监控页与 SSE 实现。
- [`scripts/start-compose.sh`](scripts/start-compose.sh)：等待 Colima 后启动 Compose。
- [`/Users/papa/Library/LaunchAgents/com.papa.n8n-compose.plist`](/Users/papa/Library/LaunchAgents/com.papa.n8n-compose.plist)：登录自启动配置。
- [`.env`](.env)：仅本机私密变量，禁止提交或分享。
