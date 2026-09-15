# 单一平台恢复验收（2026-09-10）

## 已验收边界

正式 release：`20260910T125638-07107d68cc67-dirty`。
内容清单 SHA256：`6406ced69f083b46273f5c63d1fdd869a00b0056a965fd3820df1daf774dfe4b`。
部署位置：`G:\StockPlatform\current`，正式数据为本机 PostgreSQL；公网入口 `https://stock.toufai.top`。

| 实际检查 | 结果 |
| --- | --- |
| PostgreSQL空库、指数日期领先、恢复后日期、指定历史日期 | 4项通过，隔离临时表回滚 |
| PostgreSQL业务blocked保存、修复重试、成功去重 | 3项通过，验收记录已回滚 |
| 9月7日个股覆盖 | 5022/5253，95.60% |
| 9月8日个股覆盖 | 5065/5253，96.42% |
| 9月9日个股覆盖 | 5116/5254，97.37% |
| 三日补采、完整刷新、九策略及汇总发布 | 实际完成；各10份报告，非100%数据覆盖 |
| 本机owner、adapter、报告文件一致性 | 三日各14项通过，回执在正式报告目录 |
| 公网与本机同轮结果 | 13:01三日run_id、日期、策略正文SHA256与owner/adapter/文件完全一致；`G:\StockPlatform\reports\single-platform-readback-20260910.json` |
| 最终release再次执行9月9日 | 13:00完成，execution `65fe23b34f554b38a984a2e558a29a2e` |
| 服务存活 | 13:00 owner/adapter实际HTTP健康，不单凭旧状态文件 |
| 公网板块页 | 旧8010服务停止后通过真实Edge无头浏览器验收；204板块、图表和行业/概念切换正常 |
| 旧系统停用 | 28个旧Windows任务禁用、9个旧Codex任务暂停；旧服务器service inactive/disabled |
| 归档与研究迁移 | 见CUTOVER_20260910.md的导入数及压缩包SHA256 |
| 自动化入口 | GUI无控制台宿主，代码/工作目录均指向G盘current |
| 回归 | pytest 1719 passed / 84 skipped / 48 subtests passed；发布构建通过 |

## 尚未验收或未完成

- 代表候选公司复核：9月9日0/7，不把扫描报告发布当作完整买入建议。
- 券商持仓同步：旧任务仍暂停，本次没有操作MuMu；旧工具不可因已有归档而直接删除。
- 朋友端301条兼容入口：仍返回422；本机部署成功不等于朋友端完全可用。
- 今日16:40自然调度尚未发生；真实手动执行证明当前代码与环境能跑，不能保证未来没有新故障。
- 部分数据缺口、事件策略缺失已按实际口径保留，不用填零或替代数据冒充完整。

可复验入口：`scripts/verify-equity-control-recovery.py`、`scripts/verify-short-term-lanes.py --date YYYY-MM-DD --reports-only`、`scripts/verify-single-platform-readback.py`、`scripts/verify-sector-heat-ui.mjs`。
以上验收不能被概括为“全部迁移业务和交易策略已经正常”。
