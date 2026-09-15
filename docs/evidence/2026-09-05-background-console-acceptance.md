# 后台任务弹窗修复与实机验收

时间：2026-09-05，Asia/Shanghai。范围：股票平台隧道与网页运行时的 Windows 定时任务；不代表全部股票分析业务已通过验收。

## 结论

两项任务已采用原生无控制台启动器恢复运行。最终生产环境窗口监测覆盖部署启动、真实断线后的定时恢复以及下一轮重复调度：**零终端显示事件、零终端前台切换事件**。

- 活动版本：`20260905T011616-07107d68cc67-dirty`。
- 生产入口：`G:\StockPlatform\current`。
- 任务：`trading-hareness-shared-peer-tunnels`、`trading-hareness-dashboard-runtime`。
- 两者任务入口均为 `scripts\windows\bin\stock-background-host.exe`，不再直接启动控制台 PowerShell，也不依赖窗口出现后再隐藏。
- 01:26 最终检查：两任务 Running；对应 SSH 隧道只有 1 个实例（PID 40160）；本地 5680、5681 健康检查均 HTTP 200。未留下临时验收任务。

## 原因与修复

原隧道任务每两分钟启动控制台 PowerShell，`-WindowStyle Hidden` 不能保证首帧没有窗口。重复触发周期结束会停止任务父进程，但遗留监督进程和 SSH 仍占据远端转发端口；新一轮 SSH 因端口占用退出，形成反复启动、闪窗、退出。

本次修复包括：

1. 任务首进程改为 Windows GUI 子系统程序；所有子进程使用 `UseShellExecute=false`、`CreateNoWindow=true`。
2. 使用 Windows Job Object 管理父子进程生命周期；停止任务时回收所属进程，避免遗留隧道。
3. 文件锁保证单实例；重复触发不覆盖有效运行状态，不重启现有隧道。
4. 取消重复周期边界的强制停止；保留定时故障恢复。
5. PostgreSQL、Python、SSH 的后台启动与健康检查同样走无控制台封装，具备超时、退出码和日志。
6. 发布失败可使用 `-KeepStoppedOnFailure` 保持任务停用，不自动重新启动未验收的旧版本。

## 真实验收证据

证据根目录：`G:\StockPlatform\logs\runtime\acceptance`。

| 验收项 | 实际结果 | 证据文件 |
| --- | --- | --- |
| 临时真实计划任务：启动、重复触发、周期边界、停止、错误路径 | 全部通过；停止后子进程回收；无错误弹框；0 个终端事件 | `silent-task-18b94c9eedcd46e6b284359e084fb819\result.json`、`windows.json` |
| 生产启动窗口监测 | 01:18:51.700–01:20:28.441，0 个终端事件 | `native-host-activation-windows-20260905.json` |
| 生产全程窗口监测 | **01:16:06.011–01:25:55.149，实际约 9 分 49 秒**，事件列表为空 | `native-host-production-windows-20260905.json` |
| 真实故障注入与定时恢复 | 主动断开 SSH 后，01:22:05 自动恢复；远端 HTTP 200；再观察 130 秒，跨过 01:24 调度点，仍只有一个实例 | `native-host-shared-tunnel-recovery-20260905.json` |
| 开发与生产关键文件一致性 | 12 个关键文件哈希一致，包含原生启动器及恢复测试脚本 | `native-host-runtime-hashes-20260905.json` |
| 完整发布 | 发布成功；后端 1707 项通过、84 项跳过；前端类型检查与构建通过；运行时进程测试通过 | `publish-native-host-20260905.log` |

窗口监测使用只读 Windows 显示/前台事件钩子，与用户 Explorer 位于同一桌面会话；不移动、显示或聚焦用户窗口。JSON 中 `seconds` 是请求时长，实际监测时长以 `started_at` / `ended_at` 为准。

## 没有被隐去的失败

前一轮 WScript/Hidden 方案的完整生产部署监测捕获了 2 次终端前台事件，因此该轮没有被接受为修复成功，相关任务再次停用，随后替换为当前原生 GUI 启动器。旧证据 `production-windows-20260905.json` 保留。

恢复验收脚本早期还有 XML 默认值读取、PowerShell 空列表展开两项测试程序问题，已修正并用生产真实恢复重新验证；失败记录亦保留。

## 后续定位

若再次出现闪窗，应先停用对应任务，按发生时间匹配任务事件、运行时 JSON/日志和 Windows 窗口事件，确认 PID 与父子进程链；不能仅凭单元测试通过或健康接口返回 200 就宣称无干扰。可复用 `scripts/windows/tests/measure-background-windows.ps1`、`test-hidden-scheduled-task.ps1` 和 `test-shared-tunnel-recovery.ps1`。

此次证据证明上述版本在本机完成的启动、故障恢复、重复调度场景无终端干扰，不是对任意未来 Windows/第三方程序行为的绝对保证。未修改股票策略，未操作中信或 MuMu，未执行 Git 提交或推送。
