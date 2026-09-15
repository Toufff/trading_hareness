# 推荐决策层

推荐不是扫描前五名，也不是最近聊天提过的股票。九策略负责发现；本模块负责人工注意力取舍；THS只投射已核验决定，不执行交易。没有修改策略权重。

## 单一产物与模块

- `app/recommendation_pool/rules.py`：完整候选并集、旧推荐必核、用户跟踪、确定性编译、有效期和最小差异。其最低公司研究覆盖与九策略共用 `short_term_lanes.research_queue`，不另造一套首位规则。
- `repository.py`：G盘PostgreSQL的`recommendation_pool_decisions`追加式版本，冻结context、review、result。相同输入幂等；实际扫描变化拒绝发布。发布时将已完成的推荐研究同步写入公司证据账本，并在不改变扫描哈希的前提下刷新同轮策略报告。
- `report.py` / `RecommendationPoolPanel.vue`：读取同一个result，不重排；Markdown 先展示九策略总扫描和内部推荐/观察池的前后差异，再展示逐股研究；网页位于收盘复盘的多策略报告首屏。
- `projection_guard.py`：同花顺执行前读取当前正式结果，检查决策编号、动作完全一致以及用户并发修改。仅推荐、观察可被策略刷新修改；行业/ETF不在其动作域。
- `followup.py`：将实际推荐及未选对照注册到既有前瞻观察账本；真实登记时间不回填成收盘前已推荐，日线跟踪不是实际交易收益。

## 使用

使用生产`G:/StockPlatform/current/.venv/Scripts/python.exe`和同目录`scripts/recommendation-pool.py`。环境从外部runtime.env读取，不复制密钥。调用前使用ths-watchlist读取当前云端快照。

1. `prepare --date YYYY-MM-DD --snapshot <本次THS回执> --directory <G盘本轮目录>`：保存context与空review模板。读取的是完整tracking_candidates，而不是展示top5；包括旧推荐、观察、用户跟踪。市场/参数/完整名单有输入hash。
2. 对完整并集做量价/板块比较，再实际调查优先挑战者和全部必核项。填写review.json。不能将“未复核”伪写为排除。优先序是有署名的研究判断，不伪称量化最优。
3. `publish --date ... --directory ... --review ...`：逐股校验并追加保存，写decision.json、推荐决策.md；Markdown 是同一轮扫描+池子差异的总交付，不再要求读者手工拼接两个文件。真实登记前瞻跟踪。部分研究有效仍保留展示，缺少旧推荐复核时禁止整体覆盖THS，明确指出股票及字段。
4. 用户有自选同步授权时，重新snapshot，再`plan --date ... --directory ... --snapshot ...`生成ths-plan.json。只能将此计划交给ths-watchlist预演/apply；不手写策略推荐差异。
5. 比对DB/owner/adapter/public的decision_id、报告和THS云端成员、远航版两份缓存。云端和缓存不证明手机UI；不要声称手机验收。

正式策略同步必须purpose=strategy_refresh。用户直接指定某股增删允许purpose=explicit_user_request，必须记录user_request_reference，不能冒充策略结论。更改板块/ETF不强制公司推荐流程。

## review.json合同

根字段：context_hash、author、market_assessment、attention_budget（人工阅读上限，默认5，不是组合仓位规则）、items。

每股：symbol、name、data_date、decision（recommend/observe/exclude）、stage（accumulation/initial_breakout/strong_pullback/post_limit/other）、why_now、comparison、invalidation、business、company_risk、sector_assessment、sources（url,published_date）。
推荐另需priority（不重复的编辑顺序）、trigger、peer_comparison、sector；同板块多只需same_sector_reason，不机械一板块一只。`display_observation:true`才把普通研究对象新增到THS观察，避免研究越多前台越臃肿。
移除旧观察需old_thesis、invalidated_evidence、alternative_value_review；用户跟踪不通过此字段取消。推荐降级默认保留观察。

## 更新与失败

同日存在旧式base-start与完整九策略两类结果时，读取完整九策略优先，不能仅凭updated_at让缺少strategy_lanes的旧式结果覆盖它。交易日期仍优先：不能用昨日完整报告冒充今日。完整策略的新失败记录照常显示，不回退同日旧成功冒充无故障。

新扫描改变输入hash时旧推荐标stale；超过交易日历确认的下一交易日15点标expired。读接口不联网研究、不自动跑模型、不以新扫描completed冒充推荐已完成。完成推荐研究仍需要按本流程实际执行的agent；脚本只冻结证据和校验，不宣称能自动替代公司研究。调度回执单独显示recommendation_status和decision_id，不再把扫描成功当作推荐已更新。

## 参考与验收边界

- Qlib signal_strategy.py：借鉴信号/决策/执行分层和旧项与新项一起比较；不照搬TopK投资组合参数。https://github.com/microsoft/qlib/blob/main/qlib/contrib/strategy/signal_strategy.py
- Freqtrade PairList：借鉴全量候选的模块化筛选和显式刷新，不照搬数字货币策略。https://www.freqtrade.io/en/stable/plugins/

测试覆盖完整候选、旧推荐复核、重复行业解释、观察保护、陈旧输入、手写计划拒绝、并发编辑、确定性及真实API/客户端回读。工程验收不是收益证明，也不保证下一交易日一定出现买点。
