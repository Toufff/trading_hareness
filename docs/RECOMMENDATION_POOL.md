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

1. `prepare --date YYYY-MM-DD --snapshot <本次THS回执> --directory <G盘本轮目录>`：保存context与已预填的review模板。读取的是完整tracking_candidates，而不是展示top5；包括旧推荐、观察、用户跟踪。市场/参数/完整名单有输入hash。同时写出：
   - `review-template.json`：`items` 为每个 `required_reviews` 生成骨架（编辑字段留空、`decision` 为空串，`data_date`/`name` 由系统填好），并附 `recommendation_note` 模板；
   - `note-templates.json`：`{symbol: note_template}`，覆盖**全部**候选，临时提拔某只股票不需要重跑 prepare。
   回执打印 `review_template_items`、`note_templates`、`sector_overview_sectors` 计数。
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

### 推荐说明（recommendation_note，每只推荐必填）

主观判断可以偏离扫描排序，但不能不看排序、不做对比。context 中 `sector_boards` 按板块列出全部同轮候选，并以各策略内最佳相对名次 `(rank-1)/population` 排序；这只是对照参考，不是新的跨策略分数，多策略命中也不加分。所有字段的模板由 prepare 预填（见上），作者只负责回答。

- `lane_rankings`：[{lane, rank, population}]，必须与 context 中该股的策略名次完全一致。模板已按扫描填好，这条校验现在防的是发布后篡改，不是“有没有看过”；九策略均未入选（例如旧推荐）时为空列表并在 rank_assessment 说明。
- `rank_assessment`：这些名次如何被使用，偏离排序的理由。
- `sector_peers`：[{symbol, why_not}]。必答集合＝**同板块前3 ＋ 排在它前面的全部候选，上限6只**；同板块可对照的不足3只（板块未知、板块缺失或板块太小）时，按同一相对名次键从全市场全部候选**跨板块补位**到3只。`ranking_reference.required_peers[].scope` 标出 `sector` 还是 `global`，`outranked_count` 给出同板块排在它前面的数量（不设上限）。可以多答，但必答一只都不能少。
- `sector_view`（每只推荐必填）：{assessment: strong|neutral|weak, trend, volume_price, proxy}。要判断的是**整个板块**的走势与量价，不只是同轮候选那几只。
  - 系统有该板块聚合（见下 `sector_overview`）时，`proxy.kind` 可为 `members`（无需链接）或 `etf`；
  - 系统**没有**该板块聚合时，`proxy.kind` 必须是 `etf`，带 A 股 ETF 代码（`5xxxxx.SH` 或 `15xxxx-19xxxx.SZ`）、名称、https 链接和不晚于数据日、且在30天内的 `published_date`；
  - `assessment='weak'` 时必须另写 `weak_sector_entry_reason`；系统判定 `relative_strength='weak'` 而作者不判弱时必须另写 `sector_disagreement_reason`。
- `information_checks`：[{topic, finding, url, published_date}]，至少一条发布于数据日前30个自然日内的新信息（公司公告、行业数据、新闻均可），不能只复用旧定期报告。
- `entry_reason`：最终为什么进入推荐；`priority_reason`：为什么排在这个优先级。
- `carry_over_reason`：上一轮已在推荐中的股票必填，说明它为何仍优于本轮新挑战者；“原推荐续审”不构成理由。
- 所有自由文本（rank_assessment、entry_reason、priority_reason、carry_over_reason、每条 why_not、sector_view.trend、sector_view.volume_price、weak_sector_entry_reason、sector_disagreement_reason）**strip 后不得少于20个字符**；过短记 `recommendation_note_reason_too_short:<字段>`（对手为 `why_not:<symbol>`）。

校验一次性给全：该股的全部问题收集为一条 `recommendation_note:<code>;<code>;…`，不再一次只暴露一个错误让作者反复试发布。只有整块 `recommendation_note` 缺失才单独报 `recommendation_note_required`。板块相关错误码：`recommendation_note_missing_sector_view`、`recommendation_note_invalid_sector_view`、`recommendation_note_sector_etf_proxy_required`、`recommendation_note_invalid_sector_etf_proxy`、`recommendation_note_missing_weak_sector_entry_reason`、`recommendation_note_missing_sector_disagreement_reason`。

发布时系统另写 `ranking_reference`（策略名次、板块位置、`outranked_count`、必答对手及其 scope/位置/量价、`sector_overview`、全市场 `market`），报告与网页并排展示对手的系统位置、10日涨幅和5日净流入占比，不由作者填写。同时把 `information_checks` 与 ETF 代理来源按 url 去重合并进该股 `sources`（带 `kind`），经 `short_term_lanes.reviews` 落入 `quant.market_events` 公司证据账本——为这次决策查到的东西必须留痕，不能只出现在报告里。账本仍要求至少一条一手公告或其披露镜像，研究类来源不替代它。缺任一项该股记入 errors，推荐池为 partial、禁止同步。新合同之前 prepare 的 context 不能发布，需重新 prepare。

### 板块整体（扫描侧 sector_overview）

`short_term_lanes.screen` 输出顶层 `sector_overview`：成分数≥5 的每个板块（不含 unknown）给出 {sector_key, label, members, up_fraction, return10_median, change_median, limit_up, latest_breadth, recent_breadth, breadth_acceleration, median_change_3d, flow_3d, stable_leaders, relative_return10, relative_strength}，统计的是该板块**全市场成分**，不是本轮候选。`relative_strength` 只有 weak/neutral/strong 三个**参考标签**：10日中位低于全市场且上涨占比不足一半为 weak；高于全市场、上涨占比过半且广度加速非负或3日资金为正为 strong；其余 neutral。它不是评分、不是排名、不预测收益。盘中与盘后同一函数，两边都有。它不进 `_scan_evidence`，因此 `scan_hash` 不变，已存决策不会因此变 stale；`intake` 把它复制进 context 并挂到 `sector_boards[key].overview`。

## 更新与失败

同日存在旧式base-start与完整九策略两类结果时，读取完整九策略优先，不能仅凭updated_at让缺少strategy_lanes的旧式结果覆盖它。交易日期仍优先：不能用昨日完整报告冒充今日。完整策略的新失败记录照常显示，不回退同日旧成功冒充无故障。

新扫描改变输入hash时旧推荐标stale；超过交易日历确认的下一交易日15点标expired。读接口不联网研究、不自动跑模型、不以新扫描completed冒充推荐已完成。完成推荐研究仍需要按本流程实际执行的agent；脚本只冻结证据和校验，不宣称能自动替代公司研究。调度回执单独显示recommendation_status和decision_id，不再把扫描成功当作推荐已更新。

## 参考与验收边界

- Qlib signal_strategy.py：借鉴信号/决策/执行分层和旧项与新项一起比较；不照搬TopK投资组合参数。https://github.com/microsoft/qlib/blob/main/qlib/contrib/strategy/signal_strategy.py
- Freqtrade PairList：借鉴全量候选的模块化筛选和显式刷新，不照搬数字货币策略。https://www.freqtrade.io/en/stable/plugins/

测试覆盖完整候选、旧推荐复核、重复行业解释、观察保护、陈旧输入、手写计划拒绝、并发编辑、确定性及真实API/客户端回读。工程验收不是收益证明，也不保证下一交易日一定出现买点。
