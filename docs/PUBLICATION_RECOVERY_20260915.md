# 2026-09-15 自动发布一致性修复

## 故障证据

9月14日22:10和22:40的正式任务均在publication_readback失败。CLI先独立扫描写文件，POST入口再扫描落库：information_cutoff、governance_input哈希、消息age/stale字段与report_bundle输入哈希不同。不是应忽略的浮点误差。

上一轮消息验收使用`verify-event-research-live.py --run-scan`，该辅助工具额外把API结果导出，掩盖了定时入口缺少这一步的问题。不能再用该局部入口证明自动任务成功。

## 修复与验收入口

- 正式任务先collect-only，再POST只扫描一次，随后按预期run_id/日期/哈希导出已持久化结果，最后原样严格比对。
- 发布失败记录failed checks和差异字段路径，不输出值，不忽略消息时点差异。
- `scripts/windows/test-post-close-publication-live.ps1 -TradeDate YYYY-MM-DD`复制正式任务的静默宿主/action/运行身份，创建一次性非循环验收任务，仅附加已结算日期和Force。验收任务退出0、业务completed、当次报告校验通过才算通过；finally删除测试任务，不修改正式调度。
- 真实回执在G:/StockPlatform/reports/publication-acceptance-*.json。历史日期回放不等于未来定时执行或盈利证明。

## 额外消息阻断

22:40消息状态为failed/ValueError，旧快照保留17个事件。9月15日实际复跑复现`Invalid symbol`，并非API认证失败。模型输出有不满足A股证券代码格式的关联，旧校验因此丢弃整轮事件。

只在模型输出边界隔离这些不明确的A股关联，不猜代码；海外/行业事实仍保留在事实与传导文字中。提示词明确symbols仅A股，剔除动作单独记录。引用、推理字段和其余股票校验不放宽。后续验证失败把安全failure_code一起落库，不能只留下ValueError。

生产复验另抓到Invalid importance。显示优先级的数字字符串/整数浮点按无损方式规范化，非法分值只降到最低显示优先级1并留记录，不猜测重要性，不增加交易权重；事实正文、引用ID和传导理由不修改。消息CLI业务失败退出2，禁止只因完成写文件而退出0。

本次没有更改策略权重、买入授权、券商信息或同花顺自选。
