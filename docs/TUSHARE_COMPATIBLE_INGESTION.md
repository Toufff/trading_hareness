# Tushare 兼容数据接入

本机 `quant-research` 已接入客户提供的 Tushare Pro 兼容主服务。Token 和 API URL 仅存在于本机 `.env`，容器不记录、前端不读取、n8n 工作流 JSON 也不保存它们。

## 已开放接口的接入方式

完整白名单定义在 [`quant-service/app/tushare_catalog.py`](../quant-service/app/tushare_catalog.py)，运行时可查：

```bash
curl http://127.0.0.1:5681/api/v1/providers/tushare/catalog
```

供应商书面列出的 109 个接口均可经统一的受控入口读取并写入 `quant.tushare_raw_records`。平台还登记了已实测竞价接口、官方高价值积分接口、完整实时家族和离线分钟接口，当前比较库存为 200 项。目录登记不是可用性承诺；每个接口会分别显示主源和超级源的 `declared`、`verified`、`empty`、`unsupported` 或 `failed` 事实：

```bash
curl -X POST http://127.0.0.1:5681/api/v1/providers/tushare/fetch \
  -H 'content-type: application/json' \
  -d '{"api_name":"moneyflow","params":{"ts_code":"000001.SZ","start_date":"20260701","end_date":"20260717"}}'
```

该入口强制执行：接口白名单、最多 45 天在线时间窗、每次最多 3,000 行、请求哈希幂等、fetch run 状态与原始行哈希。响应超过本地行数上限会标记为成功的 `partial` 并验证接口能力，不再误记为供应商故障。历史分钟数据不会走这个接口；应从用户提供的离线文件按批次导入。

## 历史分钟文件导入

不要让服务在线回补大体量分钟数据。将供应商给出的 CSV 放到量化容器挂载卷的 `offline/` 目录，再让服务流式读取；文件不会进入 n8n 执行二进制、飞书 adapter 内存或 PostgreSQL 的原始 Blob。

CSV 必须有标题行，并包含以下列：`ts_code`（或 `symbol`）、`datetime`（或 `bar_time`）、`open`、`high`、`low`、`close`；可选 `volume`（或 `vol`）和 `amount`。没有时区的时间会按中国标准时间解释。文件名只能是普通 `.csv` 名称，避免路径穿越。

```bash
# 第一次使用时创建持久卷中的受限导入目录（容器名以 docker compose ps 为准）
docker compose exec -T quant-research mkdir -p /var/lib/quant/offline

# 把离线文件复制到 quant-research 的持久卷
docker compose cp /safe/local/path/minutes.csv quant-research:/var/lib/quant/offline/minutes.csv

# 服务以 1,000 行为批次流式写库，文件 SHA-256 是幂等键
curl -X POST http://127.0.0.1:5681/api/v1/market/minute/import-offline \
  -H 'content-type: application/json' \
  -d '{"file_name":"minutes.csv","source_name":"provided-history"}'

# 查看导入状态、接受行和拒绝行；不会显示文件内容或任何 Token
curl http://127.0.0.1:5681/api/v1/market/minute/imports
```

单个文件默认和最大都是 5,000,000 行；不合格行会被计数并使导入标记为 `partial`，不会中断其他有效行。完全相同文件再次提交返回 `unchanged`，不会重复写入。

## 已标准化的策略输入

以下 API 除了保留原始行，还会写入规范化表：

- `trade_cal` → `market_trade_calendar`
- `stock_basic` → `instruments`
- `daily`、`index_daily` → raw observation 与 `canonical_bars_daily`
- `adj_factor` → `daily_adjustment_factors`
- `daily_basic` → `daily_fundamentals`
- `stk_limit` → `daily_trade_limits`，并回写 canonical 涨跌停价
- `suspend_d` → `security_suspensions`

盘后 `/api/v1/pipeline/daily` 会先拉取日线，再同步同日期交易日历、估值、复权、涨跌停和停复牌。快照同时要求基准、个股日线、交易日历、估值及涨跌停控制数据齐备；任一缺失即 `blocked`。

## 查询与运维

```bash
# 看某个接口的原始记录（不包含 Token）
curl 'http://127.0.0.1:5681/api/v1/providers/tushare/raw?api_name=daily_basic&limit=20'

# 查看 provider 成功、失败和熔断状态
curl http://127.0.0.1:5681/api/v1/providers/health
```

对财务、资金流、题材、龙虎榜等数据，当前先保持 raw-first，以防字段口径未确认时污染策略层；确认策略特征定义后再增加对应 canonical 表和质量规则。

## 板块与行业资金流

同花顺板块目录通过 `ths_index` 的 N/I/R/S/ST/BB 六个类型顺序同步，当前实测共 1,481 个板块。`ths_member` 已用 `883300.TI` 验证返回 319 个成分；平台要求显式 `member_offset` 与最多 50 个板块的批次，避免一次拉取全部板块成分。`moneyflow_ind_ths` 已验证可按交易日返回 90 条行业资金流，并规范化保存行业涨跌、净流入、买卖额、公司数和领涨股。

板块目录不等于成分全量完成。只有 `sector_membership_history` 中已同步的关系才能用于个股到板块的归因；其余板块继续以目录和原始证据状态展示。

概念扫描使用 `moneyflow_cnt_ths`（同花顺概念资金流）和 `limit_cpt_list`（概念涨停强度）。对净流入靠前概念，候选构建会逐个拉取 `ths_member`，再与同日 `limit_list_ths` 涨停池按股票代码严格相交；候选表保留两份原始行，不以涨停原因文本做归属推断。该扫描仅用于研究候选，不直接提升为推荐。

## 权限边界（已实测）

“15000 积分”不能被理解为官方 Tushare 的所有产品权限。两项集合竞价接口已验证；13 个股票、ETF、指数、行业和期货实时接口按供应商说明仅在相应交易时段有效，盘外响应不能作为未授权证据。主服务标准协议和超级源 SDK 路径会在工作日 10:00 用最小请求分别验证并保留原始响应。验证前实时数据不参与推荐决策。完整矩阵见 [Tushare 官方扩展与双源能力矩阵](TUSHARE_OFFICIAL_CAPABILITY_MATRIX.md)。
