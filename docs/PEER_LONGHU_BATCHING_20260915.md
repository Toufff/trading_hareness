# 2026-09-15：共享 Longhu 自动分批与公网配置路由修复

## 原因

两个告警彼此独立：

- 公网 `/api/config` 没有精确转发到 Windows dashboard adapter，落入服务器旧的通用 `/api/` 后端并返回 502。本机 `127.0.0.1:5680/api/config` 始终返回 200。
- lightServer 的量化容器仍基于 2026-09-02 代码；其中 `/licensed/longhu/quotes` 把上游单批 300 只的物理限制错误实现为整次逻辑请求最多 300 只。第 301 只触发 HTTP 422。Windows 当前源码已经实现自动拆分，但此前没有部署到远端镜像。

## 实施

- nginx 新增精确 `/api/config` 路由到 `127.0.0.1:15680`，并将该端点加入公网分流验收和发布门禁测试。
- 远端从既有 `pool-recovery-20260914` 镜像派生最小镜像 `longhu-batching-20260915`，只替换 `app/routers/longhu_reads.py`；既有空池恢复修复仍在。
- 两个远端量化服务只变更镜像；环境、命令、端口、网络、卷、数据库与 db-tunnel 均未变更。失败路径会恢复 compose 备份并重建原镜像。
- 完整共享探针发生 HTTP 拒绝时，回执现在记录阶段、状态码和服务端 JSON detail，不再只输出 Python traceback。

## 真实验收

- 公网 `/api/config`：HTTP 200，返回 6 个源路由。
- 公网分流：`/health`、`/api/config`、adapter 兼容路由和 owner `/api/v1` 路由均通过。
- 共享 API：未带 key 返回 401；目录返回 200，8 个目标、89 个示例。
- 通用历史请求 301 条拆为 `[300, 1]` 两次物理调用。
- 兼容 quotes 请求 301 只拆为两次物理调用，HTTP 200；测试符号多数不存在，因此返回 64 行不等于遗漏有效标的。
- quote、市场广度和公开源三个真实调用均为 HTTP 200。
- 两个量化容器健康，运行镜像均为 `trading-hareness-peer-quant-research:longhu-batching-20260915`。

远端证据：`/home/stockpeer/.local/share/trading-hareness/hotfixes/longhu-batching-20260915/deployment.json`。
回滚备份：`/home/stockpeer/.local/share/trading-hareness/incident-backups/20260915T115203-longhu-batching/`。

本次验收证明这两个已复现故障关闭，不证明所有外部提供方或协作者其他主机永久可用。
