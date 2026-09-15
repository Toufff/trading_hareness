# 2026-09-14：同机公网绕行导致数据库间歇超时

## 原因及证据

上一轮空池修复仍保留；本次另一个故障是 `db-tunnel` 从 lightServer 的容器
连接 **lightServer 自己的公网 IP**。这条公网路径出现严重 TCP 重传和发送队列积压，
数据库大响应和小查询共用 SSH 传输，导致小查询乃至认证超时。

- 故障期间主机直接访问 15432，连续 9 次完成真实认证及 SELECT 1。
- 公网 SSH 的发送队列约 1.7 MB，累计重传超过 60 MB。
- `ip route get 47.110.79.189`：经 `172.29.63.253 / eth0`；
  `ip route get 172.29.57.199`：本机 `lo`。
- 两条全新 SSH 连接同时读取相同 3 MB：公网 16.613 秒，内网 0.430 秒。
- 不能把这个证据解释为 Windows 上游转发坏了；也不能声称已证明云厂商限速的具体配置。

## 已上线，18:42 CST 切换

仅重建 **db-tunnel**，不重启 Windows API/PostgreSQL/上游 SSH，不重建量化容器。

- SSH 目标：`172.29.57.199:3535`。
- 已验证主机身份：`HostKeyAlias=[47.110.79.189]:3535`。
- `StrictHostKeyChecking=yes` 保持开启；不是跳过密钥校验。
- 仍转发原来的 15432/15681，不变更对外 15682/15683 地址、权限、数据卷或策略模式。
- 远端现有 bridge 拓扑保持不变，`PEER_LOCAL_BIND_ADDRESS=0.0.0.0` 仅维持原容器内绑定。
  仓库通用模板依然默认 loopback/shared-namespace，不用它覆盖现行 bridge 拓扑。
- 隧道镜像：`trading-hareness-peer-db-tunnel:private-20260914`。
- 镜像 ID：`sha256:e6e61da445fb2c942988ef49f371553ed55b1105b36203b1d276431c2935036c`。

源码修改集中在 `deploy/shared-peer/`：可选主机身份别名、连接超时、真实探活脚本、
客户端依赖和 compose 探活配置。服务器的对应构建源码也已同步，避免下次重建回退。
同机容器选择本机已验证内网地址；异机协作者的 SSH 公网入口没有改成内网地址。

## 健康检查与恢复

以前 `nc -z` 只证明监听存在。现在每次检查：

1. 使用既有私有 PG 凭据进行认证并执行 `SELECT 1`。
2. 检查 owner API 的 `/health` 返回成功。
3. 失败只输出固定阶段名，不输出密码或原始驱动细节。

PG 查询外部预算 5 秒、连接预算 3 秒、SQL 预算 2 秒；API 外部预算 4 秒。
Docker 每 15 秒检查，连续 3 次失败才标记 unhealthy，因此容器状态并非即时信号；
日志中每次检查的退出码和真实业务请求才是逐次证据。
SSH 退出由原有 `restart: unless-stopped` 拉起，没有另加守护进程或无限业务重放。

## 重复真实验收

候选隧道先并行运行，未接入正式量化服务：

- 错误密码、不存在的数据库：真实查询失败、探活退出非零，端口监听不能蒙混过关。
- SSH 进程连续 3 次退出：自动恢复 2.886 / 3.009 / 2.656 秒。
- 3 轮混合负载：48 次小查询、6 次 3 MB 读取，全部通过；小查询最大 0.637 秒。
- 不受信任的主机身份：SSH 退出 255，确认严格主机密钥校验有效。

上线后：

- 盘中量化容器再做 3 轮负载：48 次小查询、6 次大结果，无失败；小查询最大 0.561 秒。
- 实际断开/恢复正式 db-tunnel：两应用故障期间返回 503；恢复后 20.243 秒内
  10 项健康/业务接口均恢复 200，两个量化容器 ID/PID 不变。
- 研究量化容器再做 3 轮负载：48 次小查询、6 次大结果，无失败；小查询最大 0.534 秒。
- 最后 owner 转发及两量化服务共 15 项接口均 200；私网 SSH 已传输超过 100 MB，
  检查时未出现原来的 MB 级发送积压，TCP 统计无重传计数。
- 后端 1852 项测试运行，85 项按既有条件跳过，其余通过；前端类型检查及构建通过，
  保留既有的大 chunk 警告。这些代码检查不替代上述真实网络验收。

这些结果证明修复覆盖了本次公网绕行故障及已测负载，不是保证任何未来网络故障都不发生；
亦不等于所有策略研究、数据时效性及协作者媒体链路均已验收。

## 可重复验证与回滚

- `scripts/shared-peer/verify-tunnel-load.py`：在量化容器中使用既有 PG 环境，
  `TEST_PGHOST` 可选。以 `timeout 75 python ...` 运行；仅 SELECT，不写业务表。
- `test-private-tunnel-candidate.py`：仅中断明确命名的候选容器，不能改成正式容器后随意运行。
- `deploy-private-tunnel.py`：本次切换工具，要求候选验收通过并保存配置备份；
  遇到源码/配置结构变化应停止复核，不是日常自动任务。

远端证据：`/home/stockpeer/.local/share/trading-hareness/hotfixes/private-tunnel-20260914/`。
本地证据：`G:\StockPlatform\peer\acceptance\private-tunnel-20260914\`。
私有配置备份：`/home/stockpeer/.local/share/trading-hareness/incident-backups/20260914T184247-private-tunnel/`。
备份包含原 `.env`，禁止提交 Git 或公开。

回滚时恢复备份的 `.env`、compose、Dockerfile 和入口源码，将 db-tunnel image 指向
`trading-hareness-peer-db-tunnel:before-private-20260914`，用原有 compose 文件执行
`up -d --no-deps --no-build db-tunnel`。不要删除卷或重建量化镜像。
回滚会重新引入公网绕行，只用于修复自身发生回归时。
