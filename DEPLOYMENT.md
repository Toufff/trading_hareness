# 本机与服务器部署

## 本机默认部署

本项目的默认 `compose.yaml` 只把 n8n、relay 和量化 API 绑定到回环地址：`127.0.0.1:5678`、`127.0.0.1:5680` 和 `127.0.0.1:5681`。PostgreSQL、n8n runners、量化服务的容器端口均不直接对局域网或公网开放。

量化服务 `n8n-quant-research` 会在启动时创建独立的 `quant` PostgreSQL schema；它与 ingestion ledger 共用数据库实例，但不会写入 n8n 的工作流表。分析师观点只从 47 远端“市场复盘档案 API”的已解析报告同步，本机不运行 OCR、ASR 或媒体转写。它提供：

- `POST /api/v1/remote-archive/reports/import`：由 n8n 把已认证读取到的远端报告写入版本化证据账本；
- `GET /api/v1/remote-archive/reports`、`GET /api/v1/analyst-claims`：读取远端报告与规范化观点；
- `GET /api/v1/providers/health`、`GET /api/v1/research/overview`：数据源诊断与研究台总览；
- `POST /api/v1/market/bars/import`：导入校验后的日线；
- `POST /api/v1/market/sync/tushare`：仅在配置 Tushare 主源或超级源和 `QUANT_UNIVERSE` 后拉取显式股票池；日线按主源、超级源顺序回退，并保留实际 provider；
- `POST /api/v1/market/sync/baostock`：无需 token 的日线备源；只读取 `QUANT_UNIVERSE` 或远端报告中明确出现的股票代码，并自动补沪深 300 基准；
- `GET /api/v1/providers/tushare/catalog`、`POST /api/v1/providers/tushare/fetch`：主源、超级路径源和 REST 备用源的受控通用入口。请求可指定 `provider=auto|primary|super|backup`；在线请求必须在白名单内，单次最多 45 天、3,000 行；历史分钟数据只允许从离线文件导入。
- `POST /api/v1/stocks/{symbol}/study`：单标的受控研究入口。并发读取主/超级/备用 Tushare、同花顺资金流、BaoStock，以及不需 token 的东方财富、腾讯财经和新浪财经公开来源；逐来源保存原始证据、状态和健康记录。公开来源仅作低优先级交叉验证，不能覆盖已验证的主源日线。
- `POST /api/v1/market/minute/import-offline`、`GET /api/v1/market/minute/imports`：供应商提供的历史分钟 CSV 只从 `quant-research` 持久卷的 `offline/` 目录流式导入；不经 n8n、浏览器或远端下载。CSV 格式和命令见 [`docs/TUSHARE_COMPATIBLE_INGESTION.md`](docs/TUSHARE_COMPATIBLE_INGESTION.md)。
- `POST /api/v1/market/universe/sync`：每日盘前通过 `stock_basic` 刷新 `all_a` 活跃 A 股股票池。只有返回至少 1,000 只有效标的才会提交，避免供应商返回截断页时污染全市场任务。
- `POST /api/v1/market/snapshots/run`、`GET /api/v1/market/snapshots`：生成午盘和收盘的全市场快照，保存覆盖率、涨跌家数、中位涨跌、成交额、来源和质量标记。公开报价只作为补充，未验证授权实时源时快照固定为 `degraded` 或 `blocked`，不会参与推荐。
- `POST /api/v1/market/sync/full-daily`：盘后使用单次按交易日的全市场日线请求更新 canonical 日线；返回不足 1,000 只有效标的时不写入完整性结论。
- `POST /api/v1/market/sectors/sync`、`GET /api/v1/market/sectors`：按同花顺 N/I/R/S/ST/BB 分类同步板块目录；成分只能通过带 `member_offset` 和最多 50 个板块的显式批次同步，保留点时成员关系。
- `POST /api/v1/market/sectors/flows/sync`、`GET /api/v1/market/sectors/flows`：盘后读取 `moneyflow_ind_ths` 的行业横截面，保存涨跌幅、净流入、买卖额、公司数和领涨股。
- `GET /api/v1/providers/capabilities`：读取逐 provider / API 的权限与实测能力账本，`unsupported` 不会被当作可用行情源。
- 当前供应商权限的实测边界（含实时分钟未开通、集合竞价可用）见 [`docs/TUSHARE_PROVIDER_CAPABILITY_AUDIT.md`](docs/TUSHARE_PROVIDER_CAPABILITY_AUDIT.md)。
- `POST /api/v1/data-snapshots/build`：封存带时间截止的研究输入；缺少日线或存在 blocking 质量问题时返回 `blocked`；
- `POST /api/v1/outcomes/recompute`：用已可观测的后续 canonical 日线回填分析师观点结果；
- `POST /api/v1/pipeline/daily`：更新数据、重算分析师表现并生成研究候选池；
- `GET /api/v1/recommendations/latest`：读取最近一次候选池。

启动或更新：

```bash
cd /Users/papa/codebase/n8n
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:5681/health
curl http://127.0.0.1:5681/api/v1/metrics
docker exec n8n-quant-research python -m unittest discover -s tests -v
```

量化服务默认没有市场数据凭据。每日任务会按 Tushare 主源、超级路径源、BaoStock 的顺序使用同一显式股票池；没有股票池或没有已配置来源时返回 `disabled`，而不是抓取全市场或伪造数据。凭据只放在本机 `.env`：

```dotenv
TUSHARE_TOKEN=...
TUSHARE_API_URL=https://primary.example
# 新变量优先于兼容变量；三个来源均为可选。
TUSHARE_PRIMARY_TOKEN=...
TUSHARE_PRIMARY_API_URL=https://primary.example
TUSHARE_SUPER_TOKEN=...
TUSHARE_SUPER_API_URL=https://super.example
TUSHARE_SUPER_PROXY_URL=http://user:password@proxy.example:8080
TUSHARE_BACKUP_API_KEY=...
TUSHARE_BACKUP_API_URL=https://backup.example
# 客户端滚动一分钟限频预算；必须不高于供应商明确给出的上限。
TUSHARE_PRIMARY_REQUESTS_PER_MINUTE=60
TUSHARE_SUPER_REQUESTS_PER_MINUTE=30
TUSHARE_BACKUP_REQUESTS_PER_MINUTE=6
QUANT_UNIVERSE=000300.SH,600519.SH,300750.SZ
# 全市场快照默认门槛；不设置时分别为 1000、0.95、空。
MARKET_SNAPSHOT_MIN_UNIVERSE=1000
MARKET_SNAPSHOT_MIN_COVERAGE=0.95
# 仅在已经接入并实测的授权实时源写入 raw_market_observations 后配置。
MARKET_SNAPSHOT_LICENSED_PROVIDERS=broker_l1
# 默认关闭。5,539 只 A 股按 80 只/请求约需 70 个公开报价请求；
# 只有获得上游明确限频承诺后才开启，且它仍不进入推荐决策。
MARKET_SNAPSHOT_ENABLE_PUBLIC_BATCH=false
MARKET_SNAPSHOT_PUBLIC_BATCH_SIZE=80
MARKET_SNAPSHOT_PUBLIC_CONCURRENCY=2
```

主源兼容标准 Tushare 根地址；超级源复刻官方 SDK 的 `POST {base}/{api_name}` 协议并自动补充 `ts_type_name`，可通过 `TUSHARE_SUPER_PROXY_URL` 走专用代理；备用 REST 源仅启用 `stock_basic`。实时分钟仅在沪市连续竞价时段对主源和超级源做单股最小探测，成功前不进入推荐。历史分钟仍从离线 CSV 导入。完整的接口范围、在线限制、标准化表和原始数据查询见 [Tushare 兼容接入说明](docs/TUSHARE_COMPATIBLE_INGESTION.md)。

公开来源受网络出口和上游反爬/限流影响。全市场公开报价默认关闭，避免把约 70 个请求的批量刷新误当成已授权的实时数据服务；开启前必须确认供应商允许的频率。研究接口会将每一项标为 `completed`、`empty`、`failed` 或 `invalid_response`，不会将失败隐藏为无数据。

手动跑一次候选池（只会产生研究结果，绝不会下单）：

```bash
curl -X POST http://127.0.0.1:5681/api/v1/pipeline/daily \
  -H 'content-type: application/json' -d '{}'
```

## 多源量化研究闭环

研究服务把外部响应先保存为带来源和可用时间的原始证据，再生成受控股票池的特征快照。当前前端研究台提供：

- 核心股票池维护：仅池内股票会在没有分析师观点时进入量化评分，避免无边界全市场抓取；
- 单票多源研究：主/超级/备用 Tushare、东方财富、腾讯、新浪、BaoStock 的每项响应、失败和原始数据状态都可见；
- 特征与方向推荐：日线趋势、成交量、东财主力/散户资金流、基础估值和已审核分析师观点共同评分；推荐保存特征快照、置信度、风险标记、失效条件和有效期；
- 观点复核：远端报告中的公司名称无法精确映射为代码时进入待复核队列，批准并填写 Tushare 代码后才会进入股票级模型；
- 结果归因：到期推荐和分析师观点用之后的可交易日线计算方向收益、基准收益、超额收益以及最大有利/不利变动。

推荐页面仅用于研究和审计，`research_candidate` 不是交易指令。出现 `ST`、停牌、数据缺失、历史不足或涨停不可成交等标记时，模型会降低分数或给出 `watch` / `no_trade`。

因子注册、Rank IC/换手评估、A 股约束回测、Qlib/AlphaLens/LEAN/FinRL 的研究适配边界，以及 H100 离线训练分期见 [因子研究与训练路线](docs/FACTOR_RESEARCH_AND_TRAINING.md)。

首次接入市场数据前，建议先用 `POST /api/v1/market/bars/import` 导入少量经过确认的日线作链路验证；Tushare、AKShare 和公告源的历史数据必须保留来源、可用时间与复权口径，不能混用后直接回测。

## n8n 收盘后调度

生成可导入的工作流：

```bash
node scripts/build-quant-daily-workflow.mjs /tmp/quant-daily-workflow.json
```

该工作流在 `Asia/Shanghai` 工作日收盘后依次调用内部的行情同步与质量门禁、多源特征快照、到期结果归因、分析师评分卡和方向推荐接口。远端报告同步使用单独生成器，Bearer 凭据复用 n8n 已有凭据且不会出现在工作流 JSON：

```bash
node scripts/build-remote-archive-sync-workflow.mjs /tmp/text-workflow.json /tmp/remote-archive-sync.json
```

每周因子有效性与交易成本回测使用独立工作流，周六 `10:30`（`Asia/Shanghai`）运行，避免干扰收盘后行情链路：

```bash
node scripts/build-factor-research-workflow.mjs /tmp/quant-factor-research-workflow.json
```

它只读取已通过质量门禁的本地特征；历史或横截面不足时会记录 `insufficient_history`，不会生成可交易结论。

全市场补充快照已作为 `市场研究：全市场午盘与收盘快照` 工作流安装，定义保存在 [`workflows/quant-market-snapshots.json`](workflows/quant-market-snapshots.json)。它在 `Asia/Shanghai` 工作日按以下时间运行：

- `09:05`：刷新 `all_a` 活跃股票池；
- `09:10`：顺序刷新同花顺 N/I/R/S/ST/BB 六类板块目录；
- `10:00`：分别对主源和超级源进行一次单股实时分钟探测；
- `11:35`：生成午盘全市场快照；公开报价批量刷新默认关闭；
- `15:10`：生成收盘全市场快照；公开报价批量刷新默认关闭；
- `18:10`：同步全市场收盘日线。
- `18:20`：同步同花顺行业资金流横截面。

午盘和收盘报价的交易日期必须与当日上海交易日匹配。周末、节假日、停市或报价日期滞后时，快照会被记录为 `blocked`，不会被误用为当日市场状态。

在导入生产 n8n 前，先导出/备份现有工作流和 PostgreSQL；导入后只需检查 HTTP Request 节点的执行结果。量化行情 token 和远端 Bearer token 都不能写入 Git、页面或日志。

## 服务器一键部署

服务器版使用 `deploy/compose.server.yaml`，只公开 Caddy 的 80/443；内部 PostgreSQL、n8n runner、adapter 到 n8n 的 webhook、quant API 均保留在 Docker 内网。Caddy 为两个域名自动申请和续期 TLS：

- `N8N_DOMAIN`：n8n 编辑器与授权 webhook；
- `RELAY_DOMAIN`：relay/monitor 页面，Caddy Basic Auth 保护。

部署前条件：

1. 一台 Linux 服务器已安装 Docker Engine 和 Compose plugin，部署用户可运行 Docker。
2. `N8N_DOMAIN`、`RELAY_DOMAIN` 的 A/AAAA 记录已经指向该服务器。
3. 防火墙只开放 TCP 80、443 和 SSH 管理端口；不要开放 5432、5678、5680、5681。
4. 在服务器环境文件中填好已有的飞书凭据、稳定的 `N8N_ENCRYPTION_KEY`、强 PostgreSQL 密码和 relay Basic Auth；不要把本机 `.env`、备份或数据库卷上传到 Git。

在本机准备专用的服务器环境文件：

```bash
cp deploy/.env.server.example /secure/path/market-relay.server.env
# 用密码管理器填充，不要提交该文件
# Basic Auth hash: docker run --rm caddy:2.10-alpine caddy hash-password --plaintext 'your-password'
DEPLOY_ENV_FILE=/secure/path/market-relay.server.env \
  ./scripts/deploy-server.sh deploy@your-server /opt/market-relay
```

脚本只同步代码、配置模板和证书文件；它排除 `.env`、备份、日志、状态文件和依赖目录。首次将指定环境文件安全复制到服务器，后续使用服务器已有 `.env` 也可运行。脚本不会使用 `--delete`，不会清理远端数据或卷。

部署后检查：

```bash
ssh deploy@your-server 'cd /opt/market-relay && docker compose --env-file .env -f deploy/compose.server.yaml ps'
curl --fail https://relay.example.com/health
```

服务器迁移、升级或导入新工作流前，先做数据库与卷备份；不要运行 `docker compose down -v`。量化候选池是研究辅助输出，当前设计不包含券商凭据、下单 API 或自动实盘交易。
