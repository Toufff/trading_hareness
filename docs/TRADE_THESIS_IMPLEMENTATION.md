# 交易假设持续跟踪：施工与验收记录

## 范围与基线

- 2026-09-20用户批准按复审设计施工、测试、部署；执行由GPT-5.6 Sol子agent分工，主agent整合验收。
- 开工开发基线：`3dc4eec`，工作区干净；生产仍为`20260920T173705-dceb9069588e-clean`。模拟盘变更已由其他agent提交，不混入本功能修改。
- 设计：`G:/StockPlatform/reports/reviews/2026-09-20-交易假设持续跟踪与决策一致性设计稿.md`，复审后版本。
- 首次部署允许capture、shadow与advisory展示；不改九策略评分、风险预算、正式池排名或券商自选，不启用decision_binding。

## 文件责任

| 责任 | 文件/功能 |
|---|---|
| thesis_rules | trade_thesis纯合同、证据、评价、转换、呈现；共享strategy_origin；两处primary归属适配；规则测试 |
| thesis_backend | 三表及分层迁移、repository/service/read_repository、异步router、治理审查与存储测试 |
| thesis_ui | 聚焦组件/API、工作台入口、adapter映射、前端测试 |
| 主agent | 实际scan适配、现有流程挂接、CLI、集成测试、版本提交、正式发布和读回验收 |

## 验收边界

纯函数、隔离PostgreSQL、HTTP、报告、实际公网网页分别记结果，不互相替代。
华天9月15—18日仅为正反证据语义夹具，不以“应当持有到涨停”为测试目标。
晚到历史数据必须先按available_at过滤；今天建立的历史说明不标为当时的真实前瞻推荐。
至少5交易日自然影子观察不能在一天内完成，部署验收不得声称已完成此门槛。

## 实施状态

已完成规则、持久化/API、扫描挂接、报告与前端模块。首轮正式发布为
`20260920T195304-a82e80b5eb0f-clean`：生产保存51只股票的影子评价，10份报告重新导出，
owner/adapter/公网读取的评价ID与hash一致，真实浏览器桌面/手机页面读取通过。
首轮发布前完整后端3037项通过、109跳过，前端131项通过；隔离原生PostgreSQL迁移、
不可变捕获、并发、幂等与冷热迁移恢复通过。跳过项不冒充已执行。

首轮验收额外发现并修复：写接口执行器传参500、日线成交额千元到元换算遗漏、
实际指标词汇/精度展示及Windows到Linux部署脚本CRLF。写接口回归使用真实数据库执行器，
不是AsyncMock；金额回归区分日线千元与盘中元，不能统一乘1000。
最终验收回执位于 `G:/StockPlatform/reports/reviews/thesis-live-acceptance/` 与
`G:/StockPlatform/reports/reviews/thesis-live-browser/`，由部署后真实运行更新。

## 运行与接手

- 自动入口：盘后 `post_close_strategy_service.run`、盘中 `intraday_scan.runner.run`。主扫描先持久化，跟踪阶段独立失败；原扫描哈希不被跟踪副作用改写。
- 盘后同轮 `summary.trade_thesis`、九策略报告的原始假设附录与API读回共享评价ID。盘中 `receipt.json.trade_thesis` 保留阶段回执。
- 补挂已存运行：生产解释器执行 `scripts/trade-thesis.py attach --source-run-id <UUID> --output-dir <report-directory>`。这不调用行情商、不重扫、不改推荐排序。
- 只读：`scripts/trade-thesis.py show --symbol 002185.SZ`；owner `/api/v1/research/theses`，adapter/公网 `/api/research/theses`。工作台入口为“量化研究台 → 个股研究 → 交易假设生命周期”。
- 独立PG验收：`scripts/verify-trade-thesis-isolated.py`（仅创建/清理本次随机命名测试库）；线上只读验收：`scripts/verify-trade-thesis-live.py --symbol <已评价股票>`。
- 当前代码支持精确定义的单点条件；持续分钟、聚合、复杂复合语义没有实现时返回`unsupported_contract`，绝不冒充条件已满足。自由文本买点仍保留原文，不编造成精确执行信号。
- 显式账户接缝：`POST /api/v1/research/theses/{id}/bindings` 校验已存计划、账户、股票、修订、持仓快照引用与episode；`GET .../{id}/binding`读回。新增绑定不重写纪律计划。拟买入使用明确prospective-plan标识，不冒充持仓；未绑定、过时、多账户歧义均不能推导卖出股数。
- `advisory.py`在每轮评价加载已绑定纪律并生成组合投影/有效计划线。`scenarios.py`只读已批准结构化scenario，与真实纪律及可执行证据取交集；独立研究state仍单列。测算价不是最高买价，研究参考位不是硬止损，日线确认不算当日已成交。
- `effectiveness.py`按同轮真实扫描自动冻结三个研究组的原始选择、排序与生命周期状态，首个快照不随重跑刷新。`scripts/trade-thesis-effectiveness.py`读取快照，区分缺决策、未批准执行研究政策、缺成交合同；复用现有费用/可成交模拟。未批准政策不自行设TopN/买入动作。收益未知是null而不是0；原子发现仅为待审研究记录，不自动上线。
- 真实历史只读验收：`scripts/verify-trade-thesis-history.py`，9月15—18日158个候选/对照记录，153有可评价证据、5缺当前可用证据；21组真实晚到行负测试通过。北交所真实修正行在本区间没有样本，T24不得标通过。
- 回填旧扫描标记`reconstructed`；仅同日5分钟内的新扫描首次捕获可标`prospective`，起效时间仍为真实捕获时间。原始策略/范围/排名、结构、期限不随之后排序变化重写。
- 单轮有界500只；影子评价不重写策略分数或成交记录。持仓意图未绑定时明确`unbound`，不能反推真实买入动机。
- `stock-scan`、`stock-research`与`stock-discipline`用户级技能同步要求读取假设记录，区分研究结构、新买条件和实际持仓纪律；修正旧数据库路径及历史1%风险默认描述，不修改当前5%风险配置。

## 尚不声称完成的能力

- 5个真实交易日影子观察及用户批准的正式决策接管尚未发生；`decision_binding=false`。
- 当前多agent共享写密钥，审查记录强制作者/审核者不同且版本与hash一致，但身份是声明值，不是独立身份提供方签名；不得宣称密码学意义的角色隔离。
- 新模块不代替策略有效性的长期验证，也不保证避免卖飞或下一次涨停；华天案例用来检验解释一致性，而不是反向拟合一次涨停。

## 最终部署验收（2026-09-20 20:41）

- 生产源码 `67fdec0`，release `20260920T203614-67fdec0034ea-clean`；公网静态前端 `20260920123442`，其前端源码与最终release一致。后续仅本验收文档提交，不改变已部署代码。
- 全量后端 **3075 passed / 110 skipped / 835 subtests passed**；前端 **33 files / 136 tests passed**，typecheck/build通过。另行执行原生PG隔离迁移、并发、不可变性、幂等、冷热恢复及真实绑定PG测试，均通过。
- 最后HTTP验收发现GET绑定接口误传Database facade导致500，已改为executor内使用transaction connection；真实数据库执行器及三层HTTP复验均通过。后续`verify-trade-thesis-live.py`强制包含绑定GET，不只验证核心函数。
- 9月18日已存run `6d3d0b13-865d-40fb-a854-8c27472511bc`于9月20日20:40重新评价：51成功、0失败，10份同轮报告导出。不是伪造9月18日当时已有的新研究，也不是周日重扫出新行情。
- owner/adapter/公网评价ID和内容hash一致；历史截点过滤、真实POST幂等通过。跨release旧评价仍可查询；三组首次冻结hash在重跑后不变；九策略原排序hash保持 `6a1aaa15a3d2de1648d96840003476a554af458a6130b5fd7bf82b964c6cb296`。
- 公网真实浏览器桌面1440、手机390宽度读回通过，page_errors为空，截图保存在`thesis-live-browser`。共享接口运行检查20:40:37为verified。
- 三组实际快照54个事件；当前**0组可比收益**：历史执行研究政策/纯机器选择政策/成交合同不齐。执行器能力已实现，但不能称为真实策略收益验收通过；也未创建用户持仓绑定或臆造可执行买点。
- 5个交易日自然调度观察尚待发生；北交所真实报价修正历史样本未覆盖。上述不足不被代码通过率替代，正式决策接管保持关闭。
- 详细证据：`G:/StockPlatform/reports/reviews/2026-09-20-交易假设生命周期-部署验收.md`。
