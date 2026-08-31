# 百度网盘个人应用存储适配器

`feishu-adapter/baidu-pan-storage.mjs` 是一个独立的、可选的云归档适配器。它只负责百度网盘 OAuth、文件 API 和分片上传；不会把百度返回值写入交易或策略阈值，也不会改变消息去重语义。

## 已接入能力

- OAuth 授权码换取/刷新令牌，令牌以 AES-256-GCM 密文保存到 `baidu_pan_oauth_tokens`。
- 用户信息（含 v2 会员信息）、容量、设备身份/VIP 查询、目录列表、递归列表、按关键词检索、语义检索、文件元数据和下载。
- 文档/图片/视频类型列表。
- 目录创建、复制、移动、重命名、删除。
- 7 天（可配置）分享链接创建。
- 大文件本地有界 spool + 4 MiB（可配置）分片：`precreate → superfile2 → create`；快速上传会直接完成。
- Feishu relay 的大于 IM 限制资源可按 `BAIDU_PAN_ARCHIVE_PROVIDER=baidu` 或 `auto`（未配置 Feishu Drive 时）归档到百度网盘。
- 观察池、十日连板龙头影子、板块轮动/曲线、板块挖掘、涨停关联、策略决策/健康、盘后候选、模式挖掘和复盘的最新证据，可由独立归档轮询器异步写入 `market-realtime/<dataset>/YYYY-MM-DD/HH/`；归档队列有持久化幂等键和指数退避，不阻塞行情采集或策略计算。

## HTTP 入口

适配器提供以下只读/管理边界：

`GET /api/baidu-pan/status`、`GET /api/baidu-pan/oauth/url`、`POST /api/baidu-pan/oauth/exchange`、`POST /api/baidu-pan/oauth/device`、`POST /api/baidu-pan/oauth/device-exchange`、`POST /api/baidu-pan/oauth/bootstrap`、`POST /api/baidu-pan/oauth/refresh`、`GET /api/baidu-pan/user`、`GET /api/baidu-pan/device-user?device_id=...`、`GET /api/baidu-pan/quota`、`GET /api/baidu-pan/files?dir=/&type=list|doc|image|video`、`GET /api/baidu-pan/list-all`、`GET /api/baidu-pan/meta?fsids=...`、`GET /api/baidu-pan/search?q=...&semantic=true`、`POST /api/baidu-pan/share`、`POST /api/baidu-pan/manage`（mkdir/copy/move/rename/delete）。

归档运行状态：`GET /api/baidu-pan/market-archive/status`。该接口只返回启用状态、最近轮询/完成时间、队列计数和根目录，不返回任何凭据。

首次使用时，设置环境变量后访问 OAuth URL，在回调中取得 `code`，再把 code POST 到 exchange。不要把 access/refresh token 放在 URL、前端代码或日志中。个人应用的根目录通常受 `/apps/<应用名>` 约束，实际目录以开放平台返回为准。

本地冷文件迁移可使用 `scripts/baidu-pan-archive-local-file.sh`：它只通过 SSH 标准输入把有界分片送到已授权的 edge 容器，token 不离开远端加密 ledger；源文件不会被删除。每个归档目录包含按序分片和 `manifest.json`，恢复时按 manifest 顺序拼接，并在删除本地源文件前完成 SHA256、字节数和 staging restore 校验。不要把远端 token 复制到本地；如需本地直连，请在本机单独完成 OAuth 授权并保存到本地 ledger。

## 配置

参见 `.env.example` 和 `deploy/feishu-relay-edge/relay.env.example` 中的 `BAIDU_PAN_*`。凭据必须通过部署环境注入；本仓库不包含用户提供的 AppKey/SecretKey。`BAIDU_PAN_ENABLED=false` 时适配器保持关闭，现有 Feishu Drive 行为不变。启用市场证据归档还需设置 `BAIDU_PAN_MARKET_ARCHIVE_ENABLED=true`；默认每 30 秒轮询量化服务的各项最新研究读模型，按返回的 run/scan/观察时刻生成确定性键。单个快照超过 12 MiB 会进入可重试失败，不会阻塞行情采集；百度网盘只保存可重放的研究证据快照，永不作为实时策略阈值或订单输入。

## 边界与未宣称能力

百度开放平台还提供分享、预览、OCR、文档抽取、语音转写等产品能力；这些能力依赖具体产品开通和额外 scope，不会因为“文件存储适配器”启用而自动可用。当前实现覆盖个人网盘核心文件管理/传输能力，产品级 OCR/预览/ASR 应在取得对应官方权限后另建适配器和测试，避免把未验证接口接入生产链路。

官方参考：

- [百度网盘 Go SDK](https://github.com/baidu-netdisk/baidu-drive-sdk-go)
- [百度网盘 MCP（工具能力清单与 OAuth 说明）](https://github.com/baidu-netdisk/mcp)
- [百度开放平台](https://yun.baidu.com/open/platform)
