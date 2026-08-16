# 盘中生命周期、回放与概率治理

更新时间：2026-08-16。本文件只记录已经落地的契约和仍受数据门禁限制的工作；不会把研究特征或小样本回看表述为可交易策略。

## 已落地

- **连续竞价结算**：5/15/30 分钟 outcome 必须在同一中国连续竞价段内取得退出报价。午休、尾盘后的目标不借用下午或下一交易日的第一笔价格；容差届满后状态为 `unavailable`。
- **归因可重建**：结算时重算信号归因并清空概率投影缓存。旧的 `upside_research_assessment` 不再把非 EAC 信号误标为 EAC。
- **B 浪生命周期**：`shadow_confirmed + 盘中承接` 才能形成入场复核；已有持仓跌回 VWAP、3 分钟动量转负且资金/精确同源 peer 确认消失时，形成 `reduce` 复核。所有规则均经 `live_policy_gate` 处理 T+1、跌停、停牌和纸面组合限制，不下单。
- **精确板块 peer**：只在显式观察池内，使用同一 `taxonomy_key + sector_key` 的点时成员关系；不按中文名称跨源拼接，也不触发全板块成员扫描。
- **描述性状态与因子契约**：每个已生成信号保存实时状态、因子版本、输入、可用时间与缺失门禁。盘口快照只标为代理特征，未被称作逐事件 OFI/VPIN。
- **回放基础**：新增仅读取已记录事件的确定性事件时钟；排序为 `available_at → source_sequence → event_id`，产生可做黄金交易日回归的 trace digest，且不允许回放期间访问供应商。
- **不确定性展示**：现有的交易日有效样本 Beta 收缩基准率附带区间；它仍是历史条件基准率，不是经 OOF 校准的个体事件概率。

## 当前边界

- 没有历史分钟导入和 60 个独立交易日/200 个独立成熟事件前，回放、概率校准、阈值重估和策略晋级全部保持 `descriptive_only` / `research_only`。
- 腾讯主力流、公共盘口快照和板块资金流都有来源与时点限制。它们只能作为解释/确认；不得被重命名为交易所逐笔订单流。
- 分析师文本的策略可用时点仍为本地收到的不可变 `received_at`；作者自述时间只用于标记的 replay，不可倒灌到实时权重。
- 纸面组合仅提供仓位上限、敞口、回撤与 T+1 风控；没有券商账户或自动委托路径。

## 下一阶段（不自动执行历史拉取）

1. 以本地离线分钟文件填充事件账本，建立冻结的黄金交易日回归。
2. 按完整交易日做 purge/embargo walk-forward；仅对 OOF 预测计算 Brier、log loss、ECE 和可靠性图。
3. 在每个 `策略 × 动作 × 期限 × 市场状态` 满足至少 60 个独立交易日、200 个独立事件且每 cohort 30 个样本后，才允许人工评审 challenger。
4. 通过影子期、纸面组合和人工批准后，才可以改变提醒等级；任何阶段都不自动下单。

## 容量策略

研究存储总预算以 **40 GiB 为硬上限**，其中热数据库以 **28 GiB 为硬上限**；环境变量只能下调，不能提高这两个值。高频原始交叉验证在预算接近停止阈值时暂停；基础观察池报价、风险门禁、低频证据和已持久化 outcome 不会因此改写或补造。历史数据拉取仍需单独授权、范围和容量评审。

## 设计参考

- Qlib 的点时数据与研究记录模式：<https://github.com/microsoft/qlib/tree/main/docs/advanced>。
- LEAN 的 `Alpha/Insight → PortfolioTarget → Risk → Execution` 职责分离：<https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/overview>。
- 事件时间和初始化时间分离的回放原则：<https://nautilustrader.io/docs/latest/concepts/data/>。
- 概率校准应只在独立预测上评估：<https://scikit-learn.org/stable/modules/calibration.html>。
