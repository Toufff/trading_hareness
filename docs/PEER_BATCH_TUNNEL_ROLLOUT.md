# 批量隧道（batch tunnel）上线手册

本文只讲一件事：`tunnel-batch` 分支交付的「owner → lightServer 批量数据库隧道」
要怎么真正上线。设计原因、端口表、健康判据写在
[SHARED_PEER_RUNTIME.md](SHARED_PEER_RUNTIME.md)，这里不重复。

本分支**没有**改 `publish-stock-release.ps1` / `switch-stock-release.ps1`
（另一个分支持有这两个文件），也**没有**对 lightServer 做任何部署。下面第 1 节
是集成时必须由编排方补进发布脚本的改动，第 2 节是 peer 侧的实际上线步骤。

---

## 0. 现状：lightServer 上真实跑着什么（2026-09-19 只读实测）

上一轮的交接说明里写着「线上 compose 已经换成更完整的
`/usr/local/bin/ssh-tunnel-healthcheck`」——**这句话是错的**，不要再往下传。
实测结果（`ssh lightServer1`，只读，没有执行任何部署）：

| 项目 | lightServer 实际状态 | 本仓库 `deploy/shared-peer/` |
|---|---|---|
| `ssh-tunnel-entrypoint.sh` | 1694 字节，`sha256 b0aa1e02…4072c`（md5 `257573f7…`），转发地址**写死 `0.0.0.0`** | 绑定 `${PEER_LOCAL_BIND_ADDRESS:-127.0.0.1}` |
| `db-tunnel` healthcheck | `["CMD-SHELL","nc -z 127.0.0.1 5432 && nc -z 127.0.0.1 5681"]`，10s/3s/12 | `["CMD","/usr/local/bin/ssh-tunnel-healthcheck"]` |
| `ssh-tunnel-healthcheck.sh` | **不存在** | 存在 |
| `db-tunnel` 环境变量 | 只有 `PEER_SSH_HOST` / `PEER_SSH_PORT` / `PEER_SSH_USER` / `PEER_SSH_HOST_KEY_ALIAS` / `REMOTE_DB_PORT` / `REMOTE_API_PORT`；**没有 `PEER_LOCAL_BIND_ADDRESS`，没有任何 `PG*`** | 另有 `PEER_LOCAL_BIND_ADDRESS` 与 `PGDATABASE/PGUSER/PGPASSWORD` |
| `db-tunnel` 镜像 | compose **没有 `image:` 键**；运行中的镜像是构建默认标签 `trading-hareness-peer-db-tunnel:latest` | `image: trading-hareness-peer-db-tunnel:private-20260914` |
| `quant-research` 容器 | **没有 psql**；有 psycopg 3.2.6 与 `PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD` | — |

也就是说 `deploy-private-tunnel.py` 并没有落到 peer 的 shared-peer 栈上。
两条结论直接决定了 `deploy-batch-tunnel-port.py` 的写法：

1. **不能**把仓库里的 entrypoint 覆盖上去。仓库版绑定
   `${PEER_LOCAL_BIND_ADDRESS:-127.0.0.1}`，而 peer 的 compose 不设这个变量，
   覆盖后 5432/5681 会退回容器自己的 network namespace：`quant-research`、
   `quant-research-scheduler`、`n8n` 全部断连，而 healthcheck 在**同一个容器内**
   探 127.0.0.1 仍然通过——一次完全静默的 peer 全面中断。
2. 校验只能在 `quant-research` 里做。sidecar 镜像是 `openssh-client + netcat`，
   既没有 PostgreSQL 客户端也没有凭据，在里面跑 psql 永远失败。

`deploy-batch-tunnel-port.py` 现在会先只读采集 peer 的实际状态
（entrypoint 哈希 + 渲染后的 `db-tunnel` healthcheck / 环境变量键 / 镜像），
与 `KNOWN_PEER_STATES` 比对，**对不上就拒绝执行**并打印差异。
上表第一列就是其中的 `nc-legacy-20260917` 状态，对应策略是
`patch`：把批量转发块插进 peer 自己的 entrypoint，沿用它自己的 `0.0.0.0`
绑定，绝不整文件替换。

---

## 1. 发布脚本必须补的调用（由编排方在集成时施加）

本分支不改这两个文件。集成时请按下面四点修改，改完批量隧道才会随发布起来；
在此之前批量任务只能手工安装。

### 1.1 `scripts/windows/publish-stock-release.ps1`

1. **安装**：`Start-ProductionRuntime`（约第 121 行）里的 `$tunnelInstaller`
   由 `scripts\shared-peer\install-shared-tunnel-task.ps1` 改为
   `scripts\shared-peer\install-shared-tunnel-tasks.ps1`（复数）。
   - 复数脚本同样声明 `-LogonType`，现有的 `Get-LogonTypeArguments` 不用动。
   - 它默认**不因批量失败抛错**（除非传 `-RequireBatch`），所以原有的
     「降级启动」try/catch 语义不变：intraday 失败照旧抛出，批量失败只记事件。
2. **停止**：`Stop-ProductionRuntime`（约第 72 行）除了
   `trading-hareness-shared-peer-tunnels`，还要 `Stop-ScheduledTask`
   `trading-hareness-shared-peer-batch-tunnel`。
3. **禁用**：两处 `Disable-ScheduledTask` 循环的任务名列表都要加入
   `trading-hareness-shared-peer-batch-tunnel`。
4. 若 `publish-tunnel-gate` 分支的复用闸门也合进来：批量隧道任务与 intraday
   共用 `start-shared-tunnels.ps1` 与 `shared-tunnel-profiles.psm1`，
   **这两个文件必须出现在 `$script:StockTunnelAffectingFiles` 里**，
   否则闸门会漏判批量隧道的代码变更。

### 1.2 `scripts/windows/switch-stock-release.ps1`

同样三处：第 31 行附近的停止列表，第 35 行与第 65 行两处重装调用
（都换成复数脚本）。

### 1.3 验收

发布后应能看到：

- `Get-ScheduledTask trading-hareness-shared-peer-batch-tunnel` 状态 `Running`；
- `scripts\windows\get-stock-runtime-status.ps1` 里 `shared-peer-batch-tunnel`
  的 `effective_status` 为 `running`、`health` 为
  `remote_listener_open_owned_by_local_client`；
- 运行时事件里有一条 `tunnel_connection_separation_checked`
  且 `distinct = true`（即两条隧道确实占着两条 TCP 连接）。

---

## 2. peer 侧上线步骤

顺序不能换：owner 侧先把 15433 发布出来，peer 侧才有东西可连。

### 2.1 owner 侧安装批量任务

```powershell
# 先干跑，确认任务名、转发、可回收端口、健康判据
pwsh .\scripts\shared-peer\install-shared-tunnel-task.ps1 -Profile batch -WhatIf

# 真正安装两条隧道（intraday 在前、且不受批量失败影响）
pwsh .\scripts\shared-peer\install-shared-tunnel-tasks.ps1
```

安装后自检：

- `ssh lightServer1 "ss -ltn 'sport = :15433'"` 应有监听；
- 本机应能看到两个 `ssh.exe`，命令行分别带 `-R 127.0.0.1:15432:…`
  和 `-R 127.0.0.1:15433:…`；
- 复数安装脚本会自己读 `Get-NetTCPConnection -OwningProcess`，若两者共用同一条
  TCP 连接会 `Write-Warning` 并记事件。

批量健康检查失败时，`install-shared-tunnel-task.ps1` 会**先禁用批量任务再抛错**，
所以不会留下一个每 2 分钟重试同一个失败的任务。修好后需要手工
`Enable-ScheduledTask -TaskName trading-hareness-shared-peer-batch-tunnel`
或重跑安装脚本。

### 2.2 授权 key 的端口白名单

- owner 侧（`-R`）：`install-owner-tunnel-key.sh` 的默认
  `AUTHORIZED_KEY_OPTIONS` 已含 `permitlisten="127.0.0.1:15433"`。
- peer 侧（`-L`）：`provision-lightserver-rootless.sh` 的默认
  `AUTHORIZED_KEY_OPTIONS` 已含 `permitopen="127.0.0.1:15433"`。

两个脚本都**不会重写已经存在的 `authorized_keys` 条目**。老条目的失败表现完全不同：

| 缺哪一边 | 现象 |
|---|---|
| owner 缺 `permitlisten` | `-R` 是远端绑定，`ExitOnForwardFailure=yes` 让 ssh 几秒内退出，2 分钟触发器无限重试同一个拒绝——吵，但看得见 |
| peer 缺 `permitopen` | `-L` 是本地绑定，一定成功；容器 healthy、5433 端口开着，但每一条连接都被 `administratively prohibited: open failed` 拒绝——**完全静默** |

当前 `OWNER_TUNNEL_SSH_*` 四个变量未设置，两条隧道都走未受限的 `lightServer1`
别名，15433 无需额外授权即可绑定。启用受限 key 时必须先核对这两份白名单。

### 2.3 peer 侧部署批量端口

在 lightServer 上以 root 运行：

```bash
python3 scripts/shared-peer/deploy-batch-tunnel-port.py
```

脚本的执行顺序（任何一步失败都会回滚并打印回滚命令）：

1. **只读识别 peer 状态**：entrypoint SHA-256、渲染后的 `db-tunnel`
   healthcheck / 环境变量键 / 镜像。对不上 `KNOWN_PEER_STATES` 就拒绝，
   不猜、不覆盖。已经部署过则直接退出。
2. **前置断言（在任何备份和改写之前）**：`quant-research` 在跑、有 psycopg、
   有 `PG*` 凭据；本机 15433 已有监听；并先用
   `SELECT 1, inet_server_port()` 打通 `db-tunnel:5432` 拿到基线
   （必须返回 `1|55432`）。
3. **构建前**把当前运行镜像打上 `…:pre-batch-<stamp>`。
   这一步是回滚能成立的前提：peer 的 compose 没有 `image:` 键，
   `docker compose build db-tunnel` 会就地覆盖同名标签，原镜像变成 dangling。
4. 备份 `.env` / `compose.yaml` / `ssh-tunnel-entrypoint.sh` 到
   `/home/stockpeer/.local/share/trading-hareness/incident-backups/<stamp>-batch-tunnel-port/`。
5. 改 `.env`（`PEER_BATCH_DB_PORT=5433`、`PEER_BATCH_REMOTE_PORT=15433`）、
   给 compose 的 `db-tunnel` 加这两个变量、把批量转发块 **patch** 进 peer 自己的
   entrypoint（沿用其 `0.0.0.0` 绑定）。
6. 断言渲染后**除 `db-tunnel` 外没有任何服务发生变化**，且 `db-tunnel` 的
   `volumes/ports/networks/restart/healthcheck` 都没动。
7. 只构建、只重建 `db-tunnel`，等待 healthy。
8. 校验：从 `quant-research` 用 psycopg 连 `db-tunnel:5433` 跑
   `SELECT 1, inet_server_port()`，**必须**得到 `1|55432`；
   随后**再验一次 5432**，确认重建没有打断原有通路。
9. 写 `batch-tunnel-deployment.json` 并打印回滚命令
   （恢复配置文件 + `docker tag <pre-batch> <原标签>` + 重建容器）。

### 2.4 让消费方用上 5433

批量/回填/COPY 类作业把 `PGPORT` 从 5432 改成 5433（`PGHOST` 仍为 `db-tunnel`）。
盘中请求路径**不要**改：它就该留在未压缩的 intraday 连接上。

---

## 3. 回滚

```bash
# 脚本运行结束（成功或失败）都会打印这一条，原样执行即可
cp /home/stockpeer/.local/share/trading-hareness/incident-backups/<stamp>-batch-tunnel-port/* \
   /home/stockpeer/trading_hareness/deploy/shared-peer/ && \
docker tag trading-hareness-peer-db-tunnel:pre-batch-<stamp> trading-hareness-peer-db-tunnel && \
docker compose … up -d --no-deps --no-build db-tunnel
```

owner 侧回滚更简单——批量隧道是独立任务：

```powershell
Stop-ScheduledTask  -TaskName trading-hareness-shared-peer-batch-tunnel
Disable-ScheduledTask -TaskName trading-hareness-shared-peer-batch-tunnel
```

intraday 隧道、`shared-peer-tunnels` 运行时服务、其状态文件与锁文件都不受影响。

---

## 4. 尚未完成的事（交给编排方）

- 第 1 节的发布脚本改动尚未施加，因此**批量任务目前只能手工安装**。
- 批量隧道从未真正启动过：没有证据证明 lightServer 的 sshd 会为当前 key
  发布 `127.0.0.1:15433`，也还没有任何吞吐量收益的实测数据。
- `deploy-batch-tunnel-port.py` 从未在 peer 上执行过。它已经会先核对 peer 的
  真实状态再动手，但「拒绝」与「patch」两条路径都只在本地用 peer 文件的只读
  副本验证过（patch 结果 `sh -n` 通过且幂等，compose 插入后 YAML 可解析）。
- 批量任务还没有对应的 kill-and-recover 验收（intraday 有
  `test-shared-tunnel-recovery.ps1`）。装好之后值得跑一次。
