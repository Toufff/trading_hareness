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

施工中。测试数量、source commit、release、真实run/evaluation与部署后读回将在完成后追加。
