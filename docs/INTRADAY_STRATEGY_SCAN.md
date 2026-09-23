# 统一盘中九策略扫描

生产入口：`G:/StockPlatform/current/scripts/stock-intraday-scan.py`。
数据库：G盘 PostgreSQL；报告：`G:/StockPlatform/reports/intraday/<时间-运行ID>/`。
网页：`https://stock.toufai.top/intraday`，研究台复盘页也提供入口。

```powershell
& G:/StockPlatform/current/.venv/Scripts/python.exe G:/StockPlatform/current/scripts/stock-intraday-scan.py
```

午盘、下午、尾盘都用这个入口，不再二次调用旧stock-tail-scan.py。后者仅保留旧版本冻结包兼容用途。当前九策略由同一正式screen核心、同一已批准配置计算；旧四策略discover仅供旧代码测试/档案，不作为当前CLI引擎。

## 实际覆盖与解释

- 全市场窗口采集只用Longhu封装，每页最多300；午休固定11:30、盘中协商最新可用5分钟窗口、收盘后15:00明确标为闭市初始化。
- 潜伏使用今天累计资金与价格重新计算3/5/10日资金和自适应平台，不只拿昨天名单。平台内承接无需先突破。
- 启动可用供应商量比扩大初筛，须标为代理证据；没有历史同刻成交额就不声称同刻放量。回踩不因半日累计额低而获得缩量加分。
- 九策略均执行；高级形态依赖真实日K。80日日K与分钟证据各按最多128只的明确预算采集，先保障本轮九策略每路前8及正式推荐，再补高级策略预筛和旧展示名单。未被请求的候选分别计入`selection.priority_missing`、`prefilter_deferred`、`old_display_deferred`；请求成功数不能冒充本轮前排覆盖率。不是全市场逐票分钟/80日K扫描，未补对象保留缺口，不能当作不合格。
- 盘中日K是实际采集时刻的未完成日K；市场窗口、OHLC采集、实际可知时间分别存储。严格禁止收盘后日K倒填14:50历史包。超过10分钟采集时差拒绝拼接。
- GetKLineDay_W14可能在收盘后一段时间才补当天。用GetStockPanKou真实open/high/low/last补形成中的日K，并核对前收复权口径；不从分时采样价猜高低。历史根数、当日OHLC覆盖、40根就绪分开统计，上市不足40日保留short_history。
- 九策略本轮匹配与原候选跟踪单列。原候选没再次匹配，不被分钟转强升级成当前策略入选。正式策略拥挤/风险状态不能被分时越线覆盖。
- 午盘结果必须带`presentation`确定性摘要。11:30 前的有效正式推荐从`recommendation_pool_decisions`读取并按正式`priority`单列；11:30 发布的新推荐在下午扫描中接续。历史候选、新发现和当前前排只在各自策略内按状态、正式排名、成交额和代码排序。不同策略分数不可混成总分，Agent不得再从混合表临时挑股。
- 扫描、公司研究、前瞻计划、价格触发、可成交性分开。程序不授权买入；用户要推荐时必须完成优先项公司/催化核查，并按该策略给入场和取消条件。不能用“后续核查”冒充完成分析。

## 午盘正式决策

`$stock-scan-noon` 使用精确11:30的完整本轮输入做九策略扫描，再用与盘后共用的`recommendation-pool.py prepare/publish --intraday-run-id <RUN_ID>`完成公司复核和推荐/观察池决定。推荐项须有11:30分钟、至少40根日K和实际可知的公司证据；缺项保留候选并标明，不能隐形删除。决定绑定该`intraday_run_id`，仅当日15:00前有效，网页与盘中监控读取同一正式决定。盘后决策仍由已结算数据替代，不将午间形成中的日K写入已结算日线表。

`$stock-scan-inmarket` 是随时触发的盘中扫描，不自动替换正式池；`$stock-scan-aftermarket` 是已结算盘后复盘。旧`$stock-scan`只作显式兼容路由。

## 冻结、读回和复盘

input.json冻结市场、日K、分钟、事件、治理参数、实现指纹和原计划；result.json、总报告与九份独立报告来自同一输入。数据库逐项hash读回。

```powershell
& G:/StockPlatform/current/.venv/Scripts/python.exe G:/StockPlatform/current/scripts/stock-intraday-scan.py --replay <input.json>
```

回放不联网、不写数据库，须使用生成该包的release。修改实现后不能把当前模型回算称为原模型回放。

收盘初始化后自动将同日早先扫描追加到`quant.intraday_scan_reconciliations`。已有收盘任务也调用统一入口完成对照；比较失败单列，不抹掉正式收盘策略结果。可手动复验：

```powershell
& G:/StockPlatform/current/.venv/Scripts/python.exe G:/StockPlatform/current/scripts/stock-intraday-scan.py --reconcile <15:00运行ID>
```

对照只记原观察价→收盘变化、原计划创建后的价格触发、候选状态变化。没有成交证据不算成交收益；不无视T+1、费用和涨跌停。原运行与报告不覆盖。

## API和页面

`GET /api/v1/intraday-scans`（owner）和`GET /api/research/intraday-scans`（adapter）读取同轮结果，支持run_id、lane、symbol。GET不发起采集。列表不重复发送全体K线，点股票再读同run_id详情。

页面支持九策略切换、旧运行选择、名称/代码搜索、分时/VWAP、真实日K、参考线/失效线、入场情景、数据缺口及收盘对照；无运行、失败、加载均有独立状态。

```powershell
node G:/StockPlatform/current/scripts/verify-intraday-ui.mjs
```

## 验收边界

CLI是主动扫描能力；既有收盘任务接入对照，不意味着四个盘中定时节点自动改造完毕。测试证明代码、实际数据、数据库、页面和回放在测试样本上成立，不证明未来永不失败或策略有稳定收益。失败按availability/market_capture/history/formal_prefilter/strict_ohlc/formal_enriched/minutes/evaluate/reports/database_readback定位。

不读/修改券商，不运行MuMu，不触碰同花顺分组，不改变正式收盘权重和小盘因子设置。完整验收标准见INTRADAY_IMPLEMENTATION_ACCEPTANCE.md。
