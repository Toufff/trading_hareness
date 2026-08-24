# P1 无中断服务交接边界

## 当前已具备

- `quant-research-gateway` 是独立 Nginx sidecar，默认仅监听本机
  `127.0.0.1:5682`，默认回源现网 `quant-research`；回源由单独配置文件控制，
  可在预热实例已健康时热切换。
- `quant-research-preflight` 位于 Compose 的 `preflight` profile 内，默认
  不会启动；其唯一用途是新镜像的无租约预热验证。
- `deploy/compose.quant-handoff.yaml` 提供 `handoff` profile 的
  `quant-research-handoff` 候选服务。它继承现网 quant-service 的全部提供方、
  数据库、证书和存储配置，却没有发布端口，且默认
  `QUANT_HANDOFF_BACKGROUND_TASKS_ENABLED=false`。这比最小预检实例更接近
  将来实际发布的进程，但不会接收流量或申请后台租约。
- 代理禁止自动重试写请求；写入幂等仍只由调用方的 request key 负责。
- 网关对量化服务的读写超时为 420 秒，高于适配器的一键盘后 360 秒等待预算；
  这样长流程不会在服务端继续执行时先向调用者返回 504、诱发重复提交。
- `QUANT_BACKGROUND_TASKS_ENABLED=false` 可启动一个仅供预热的
  quant-service 实例：它会完成进程、数据库、schema 校验和 HTTP 客户端
  初始化，但不会创建任何 `background_loop:*` 租约，也不会执行采集、
  策略或投递循环。
- `/health.optional_background_tasks.background_tasks_enabled=false` 是预热
  模式的显式证据；空的 `runtime_loops` 在该模式下是预期行为，不应告警为
采集故障。
- `scripts/audit-live-quant-callers.sh` 从已发布的 n8n 版本和正在运行容器读取
  实际调用地址；`--require-gateway` 只在全部活跃调用方都已确认迁移后通过。
  本地 n8n 的 `publish:workflow` CLI 明确要求重启才生效，不能把它用于无中断
  发布；需要热发布时必须使用有权限的 n8n API，再运行该审计确认。
- `node scripts/verify-zero-downtime-handoff.mjs` 会校验默认回源、禁止写
  请求自动重试与 preflight 无后台任务护栏；附加
  `--active <url> --gateway <url>` 时还会只读比较两端的健康状态、循环集合和
  循环错误。任何不一致都应中止切换并保持默认回源。
- `node scripts/verify-ten-day-shadow-release.mjs --url <candidate-url>
  --require-intraday --require-no-background-tasks` 专门验证十日排行榜影子
  接口：候选实例必须无后台循环、保持 `research_only_no_orders`、返回已知
  的盘中批次及来源，并强制 `decision_eligible_count=0`。这让“最新盘后批次
  被历史门槛阻断”与“仍可展示最近完成池的盘中观测”成为可发布前验证的行为，
  而不是前端推断。
- `scripts/quant-opening-preflight.sh` 逐项要求九条运行租约，并对已经保存的
  盘中状态最多采样三次（间隔两秒）。这只消除刷新边界的读竞态；若资金流、飞书
  或盘口状态持续退化，预检仍会 fail-closed，不能带病切换。

## 尚未切换的边界

仓库内 n8n 工作流、Compose 的 n8n 进程和飞书适配器已声明稳定调用地址
`QUANT_SERVICE_URL=http://quant-research-gateway:8000`，但正在运行的容器与
已导入的 n8n 工作流不会因源码改动自动重载。当前实例仍直接访问
`quant-research:8000`；这保证本次增加网关不会改变正在处理的请求。虽然网关
已将唯一回源点独立为 `deploy/quant-research-gateway-backend.conf`，在显式
导入工作流并滚动替换调用方前，仍不能把它误称为已完成的蓝绿发布。

在明确的维护步骤中，必须先将每个调用方迁移到稳定网关地址，再为新版本提供
独立、无后台任务的预热实例，并仅在网关健康检查、数据库 schema 与所需读接口
都成功后切换可配置回源。切换后还要确认旧实例释放所有 background leases，才可
停止旧容器。整个过程不得同时运行两个拥有相同 background lease 的工作实例。

## 十日排行榜影子策略的发布边界

- 后端路由为 `GET /api/v1/research/ten-day-leader-rotation/latest` 与
  `POST /api/v1/research/ten-day-leader-rotation/run`；只返回研究证据，永远
  不产生订单。
- 前端收盘复盘页使用独立的 `TenDayLeaderRotationPanel` 展示运行状态、完整
  历史门禁与候选；旧版飞书适配器尚未切换时，读取会安全降级为“尚未部署”，
  不影响其它研究卡片。
- 新适配器映射已在候选镜像中验证。实际替换适配器应与工作流/调用方的稳定
  网关迁移安排在同一受控维护窗口；不得仅为显示该卡片而中断正在运行的群消息
  转发或 n8n 任务。
- 当前运行的适配器仍未包含该映射，且五个已发布 n8n 工作流仍直连旧地址；
  在 `scripts/audit-live-quant-callers.sh --require-gateway` 通过前，禁止替换
  主 quant-service。使用 `scripts/hot-publish-quant-workflows.mjs --apply`
  需要调用者明确提供具有 `workflow:read`、`workflow:update`、
  `workflow:activate` 权限的 n8n API Key；不得以直接改写 n8n 数据库替代。

## 候选预热（只读、可回收）

在构建候选镜像后，首选运行以下命令。它会检查候选服务没有发布端口、继承关键
运行环境、健康端点无后台任务、影子研究边界和真实盘中批次；验证后自动停止并
删除候选容器：

```bash
scripts/preflight-quant-handoff.sh
```

需要保留候选供人工检查时使用 `scripts/preflight-quant-handoff.sh --keep`。
候选服务没有宿主机端口；从容器内读取 `/health` 与影子端点，确认
`background_tasks_enabled=false`、`runtime_loops={}`、研究范围和盘中批次都
满足 `scripts/verify-ten-day-shadow-release.mjs` 的约束。人工检查完成后回收：

```bash
docker compose --profile handoff -f compose.yaml -f deploy/compose.quant-handoff.yaml \
  rm -f -s quant-research-handoff
```

只有活跃调用方审计已全部迁移到网关后，候选服务才可进入后续切流步骤；本预热
命令本身不改变网关回源、n8n 工作流或现网后台任务。

不要用 `docker compose run quant-research ...` 作为临时候选校验。该服务名会在
默认 Docker 网络中复用 `quant-research` DNS 别名，可能令已经解析过上游的
网关短暂持有临时容器地址。任何临时 HTTP 合约检查都必须以
`quant-research-handoff` 服务名启动，并在启动和回收后各检查一次网关健康。
