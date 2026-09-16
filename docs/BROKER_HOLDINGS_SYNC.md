# 券商持仓同步：主动触发、只读事实通道

本通道只在用户当前明确请求下读取当前已登录的桌面券商客户端，独立于股票池、公司研究和旧 stock-brain。2026-09-13 已完成一次真实 THS/中信手动读取、入库和 owner/adapter 逐字段读回；这是一轮验收证据，不是长期稳定性保证。

## 入口与边界

- 触发语义固定为 `manual`。原 12:00/15:15 自动同步已停用（不是 15:00 调度），不后台轮询、不自动登录、不自动唤醒 MuMu。
- 用户未指定券商时不得默认中信；先确认客户端、券商、显式 `account_key` 和掩码账户。
- 原始文件导出优先；没有可验证导出时，由 `gpt-5.6-luna` 的 Luna 子 agent 直接做桌面结构化人工读取。主 agent 只负责授权、证据验收和 CLI 编排，不把 OCR 或模型猜测当事实。
- 不得买卖、撤单、转账、融资、修改自选或确认交易；不得读取密码、Cookie、内存或认证材料。
- 不擅自 activate、抢焦点、覆盖用户正在使用的窗口或 restore 最小化窗口。用户明确授权“显示”时，才可尝试无激活显示；若必须取得焦点，先请用户允许。

## THS 窗口与截图

网上交易系统进程为 `xiadan.exe`（网上股票交易系统 5.0）。`MainWindowHandle` 可能只是 `Internet Explorer_Hidden`；主 `happ` 窗口最小化时可能有 `-32000` 矩形。hidden 交易窗的被动 `PrintWindow` 黑图不构成证据。

优先使用 `scripts/windows/broker-window-capture.ps1`。当前参数为：`-Mode list|capture`、`-ProcessIds`、`-Hwnd`、`-OutputPath`、`-ShowWithoutActivation`、`-TemporaryWidth`。`-ShowWithoutActivation` 与 `-TemporaryWidth` 都是显式 UI 动作，默认不开；临时加宽后必须恢复原尺寸。脚本只验证窗口身份、前台窗口未变化、输出文件 hash 及 PNG/JPEG 文件头，不证明图片内容；视觉完整性由 Luna 检查。UI 本轮最多 8 分钟、最多 2 次安全恢复。

## 当前 CLI 与账户确认

CLI 为 `G:\StockPlatform\current\scripts\broker-fact-sync.py`，只做 run、确认、envelope 校验、持久化和读回，不导航 UI。使用当前 release 的完整解释器：

```powershell
G:\StockPlatform\current\.venv\Scripts\python.exe G:\StockPlatform\current\scripts\broker-fact-sync.py start --phase manual --manual-user-authorized --account-key <key>
G:\StockPlatform\current\.venv\Scripts\python.exe G:\StockPlatform\current\scripts\broker-fact-sync.py confirm-account --phase manual --manual-user-authorized --account-key <key> --run-id <UUID> --envelope <envelope.json> --confirmation-record <record.json>
G:\StockPlatform\current\.venv\Scripts\python.exe G:\StockPlatform\current\scripts\broker-fact-sync.py complete --phase manual --manual-user-authorized --account-key <key> --run-id <UUID> --envelope <envelope.json> --validate-only
```

## 同花顺导出优先流程

当前实现支持同花顺常见的“扩展名为 `.xls`、内容实际为 GB18030/UTF-8 制表符文本”的导出。二进制旧式 XLS 与 XLSX 会明确失败，不能把任意 Excel 文件误读成已验证券商证据。

查询表的导出入口是数据行上下文菜单，不是顶部文件菜单：先让真实数据单元格获得焦点，`Ctrl+A` 全选并确认行已高亮，再在高亮数据行内右键，选择 `保存(&S)...`。客户端资源中的命令说明为“将查询结果保存到文件”，命令编号为 `32827`；“Excel 文件”筛选器实际可能生成 `.csv`。右键空白区域、只测试顶部菜单或在未打开上下文菜单时发送保存命令，都不能作为“不支持导出”的证据。弹出菜单和保存对话框必须用桌面级截图取证，不能只截主窗口，因为系统/自绘弹窗可能不在 `PrintWindow` 图像中。

一次完整同步应尽量导出三类原始文件：

1. 账户汇总：总资产、可用资金、股票/证券市值；
2. 当前持仓：证券代码、名称、实际数量、可用股份、成本价、市价、市值、盈亏；
3. 对账单或交割单（可选）：交易日期、证券、备注中的买/卖、成交数量/价格/金额、发生金额和各项费用。

成交记录不能替代当前持仓，持仓表也不能替代账户汇总。客户端当日已卖出的零数量行只保留在解析诊断中，不计为当前持仓。实际安装版本是否能分别导出账户汇总和持仓，仍须用一次真实 GUI 操作验收；如果账户总额不能导出，不能声称“完全文件化”，而应由本轮 UI 证据补齐或明确失败。

Luna 只负责打开只读页面并保存原始文件。金融字段由解析器重读，session manifest 只允许记录观察时间、掩码账户和已有账户绑定。示例：

```powershell
G:\StockPlatform\current\.venv\Scripts\python.exe G:\StockPlatform\current\scripts\broker-fact-sync.py prepare-export `
  --phase manual --manual-user-authorized --account-key <key> --run-id <UUID> `
  --session-manifest <session.json> --holdings-export <holdings.xls> `
  --account-export <account.xls> --trade-export <statement.xls>
```

命令生成 `export-envelope.json` 和可选的 `trade-batch.json`。`complete --validate-only` 会再次从原始文件解析并逐项比较，防止修改中间 JSON；随后正式 complete 在同一数据库事务写入持仓快照与去重成交记录，并读回持仓 API。成交表为 `quant.broker_trade_records`，同一 `account_key + trade_key` 重复导入不重复记账，内容冲突则失败。

`confirm-account` 必须有真实用户确认记录，绑定方式为 `user_confirmed_once`；不得由 agent 自写布尔断言。账户 fingerprint 可为 nullable：用户未提供完整账号时不得伪造 fingerprint；若提供，算法为 `SHA256(NFKC(casefold(broker)) + ":" + NFKC(account_identifier 去空白后转大写))`。确认后才允许本轮 envelope 进入 complete。

## Envelope、证据与验收

envelope 使用 `schema_version=ths-desktop-holdings-v1`、`trigger=manual`、本轮 run_id、显式 account_key、source、account_identity、account_binding、observed_at、account 和 positions。source 为 `ths_desktop_ui` 或经真实 parser 验证的 `ths_desktop_export`；手工 JSON 不得冒充导出。

每个证据文件必须有绝对路径、SHA-256、captured_at 和来源角色。UI 证据覆盖账户身份、账户总额、完整持仓页和列表末尾；空仓需 `explicit_empty=true`。总资产、现金、股票市值及每行名称/代码、数量、可卖数量、成本、现价、市值、盈亏均来自本轮证据，不能从旧快照、比例或模型推断。

先执行 `--validate-only`，再持久化；随后核对数据库、owner、adapter 的 snapshot/content hash、source、account、observed_at、账户字段和全部持仓行。2026-09-13 本轮证据示例：四行持仓，snapshot `ff52cde9-d1dc-47ef-966b-0faab3b1533d`，observed_at `2026-09-13T14:42:22+08:00`，receipt 在 `G:\StockPlatform\data\broker-evidence\1c3cdb69-a0e4-46d6-b028-0917d7c9c3ca\receipt.json`；私人证据不入 Git。

任何缺证据、身份不匹配、窗口/截图阻断、字段不完整或读回不一致都必须失败并写 alert；旧快照只保留 stale history，不得伪装当前持仓。市场扫描和公司研究继续独立。正式生产发布及后续稳定性仍需另行验收。
