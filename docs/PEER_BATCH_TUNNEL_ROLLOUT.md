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

批量健康检查失败时，`install-shared-tunnel-task.ps1` 默认会**先禁用批量任务再抛错**，
所以不会留下一个每 2 分钟重试同一个失败的任务。修好后需要手工
`Enable-ScheduledTask -TaskName trading-hareness-shared-peer-batch-tunnel`
或重跑安装脚本。**只有一个例外**，见下面第 3 点。

这条「失败就禁用」的规则很硬，所以判定必须站得住。健康判据的第三条
（运行时状态属于本次安装）现在看三样东西——`run_id`、`started_at`、以及状态自己的
`status`——并且在 30 秒内**反复重读状态文件**，而不是只判一次：先起来的是 ssh
客户端，supervisor 写状态文件要晚一点，判一次就会把一条正在起来的隧道判成失败并禁用。

1. **`status` 第一个判，而且对所有分支都生效。** 状态里写着
   `stop_requested` / `stopped` / `unexpected_exit` / `supervisor_failed` /
   `start_failed` 的，一律不算 fresh。这里分两种情况，报法不同：
   - `run_id` 还是上一轮的 ⇒ `previous_run_stopping`。安装脚本自己在注册任务之前
     就调了 `Request-RuntimeStop`，那一步会把同一个状态文件改写成
     `stop_requested`，所以这正是「本次安装刚刚下令停掉的那一轮」。
   - `run_id` 是**本次安装自己的新 id** ⇒ `run_ended_before_health_was_proved`。
     `supervise-runtime-process.ps1` 在进程意外退出时写的就是这个形状：新 `run_id`、
     晚于安装时刻的 `started_at`、`status = 'unexpected_exit'`。只看 `run_id` 和
     时钟的旧判法会把它当成健康——而监听探测几秒前刚刚通过，所以这条错得毫无破绽。
     上一轮把 `status` 检查只放在「`run_id` 没变」那个分支里，漏掉的就是这一种。
2. **没有「弱接受」了。** 早先的版本会在「`run_id` 还是上一轮 + `supervisor_pid`
   指的进程还活着」时按 `duplicate_supervisor_still_serving` 接受，健康标签记
   `remote_listener_open_owned_by_live_supervisor`，用来兜住
   `duplicate_start_skipped`（supervisor 抢不到 `<service>.lock` 就退出且不写状态，
   此时状态里还是上一轮的 `run_id`，而隧道是好的）。**那条分支从安装脚本里根本走不到**：
   `Request-RuntimeStop` 在注册任务之前就跑了，于是安装脚本能读到的「上一轮状态」
   一律带着停止类 `status`。代码、模块注释和两份文档一起承诺了一件代码做不到的事，
   所以这一轮把它**删掉**，而不是修补。现在的闸门就是 `fresh` 一条。
3. **拒绝 ≠ 禁用。** `previous_run_stopping` 这一种会带上 `handover_in_progress`，
   安装脚本对它调 `Stop-TunnelInstallOnFailure -KeepTaskEnabled`：安装仍然失败
   （证不出来的东西不写 `healthy`），但**批量任务保持启用**，2 分钟触发器会自己把这次
   交接跑完。这恰恰是当初发明弱接受要保护的场景，而弱接受实际上保护不到。
   其余所有拒绝照旧禁用任务。
4. **轮询只以 `fresh` 跳出，且不留兜底。** 读到本次安装自己的状态
   （新 `run_id` + 新 `started_at` + 非停止类 `status`）才提前 break；
   30 秒走完都没等到，就按**最后一次读到的判定**拒绝，不会把前面某次较弱的判定
   翻出来当兜底。

`Get-SharedTunnelSupervisorLiveness` 现在只是**回执**，不再是闸门：它仍然把
`supervisor_liveness` / `supervisor_pid_checked`（连同 `state_freshness` /
`previous_run_id` / `state_status`）写进运行时状态和 `healthy` 事件，被拒绝的安装
不写状态，就把这两个值写进失败信息里。

第一次真装完，`G:\StockPlatform\logs\runtime\shared-peer-batch-tunnel.current.json`
里应当看到 `state_freshness = state_belongs_to_install`、
`supervisor_liveness = supervisor_process_owns_this_run`。若 `supervisor_liveness`
是 `process_start_time_unavailable`（supervisor 属于另一个账户，读不到 StartTime），
现在**不会**再因此改变判定——该字段只是回执；但它仍然值得在第一次安装时核对，
因为读不到 StartTime 说明这台机器上的 supervisor 不属于当前账户，那本身就要查。

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

1. **只读识别 peer 状态**：entrypoint SHA-256、`compose.yaml` 原文 SHA-256
   （按 LF 归一化）、渲染后的 `db-tunnel` healthcheck / 环境变量键 / 镜像。
   对不上 `KNOWN_PEER_STATES` 就拒绝，不猜、不覆盖。
   `compose.yaml` 必须按**原文**钉住而不是只看渲染结果：脚本是在
   `      REMOTE_API_PORT: ${REMOTE_API_PORT:-15681}` 这一行字面量上插入变量的，
   而 `REMOTE_API_PORT: "15681"`、改缩进、改用 `env_file`，渲染出来完全一样——
   少了原文哈希，重排过的 compose 会通过状态闸门，然后在 `.env` 已经改完之后
   用一句裸 `AssertionError` 死在写入中途。
   **已经部署过则打印状态并 `exit 0`**：部分失败后重跑确认幂等是常规动作，
   不该和真正的拒绝一样返回非零。这个判断放在 `KNOWN_PEER_STATES` 查表**之后**
   （部署完成的 peer 的 entrypoint 哈希本来就不在表里），
   并且**只认三条同时成立的证据**：渲染后的 `PEER_BATCH_DB_PORT` 的**值非空**、
   peer 的 entrypoint 里确实有批量转发那一行（`BATCH_FORWARD_MARKER`），
   以及**当前正在跑的 `db-tunnel` 容器确实在转发这个端口**——
   `docker exec <container> cat /proc/1/cmdline`（entrypoint 最后是
   `exec ssh "$@"`，所以容器里的 pid 1 就是 ssh 本身），
   argv 里必须有一个紧跟在 `-L` 后面、且以
   `:<PEER_BATCH_DB_PORT>:127.0.0.1:<PEER_BATCH_REMOTE_PORT>` 结尾的参数。
   前两条都是**文件状态**，而文件是在 `docker compose build` **之前**写的：
   被 SIGKILL / OOM / 断电打断在那个窗口里的一次运行（这些都进不了
   `except BaseException` 的恢复分支）留下的正是这个形状，而容器跑的镜像里
   根本没有那条转发——旧逻辑会 `exit 0` 报告一次「已部署」，
   而它报告的那个端口根本不会应答。

   第三条证据**上一轮是 `…:batch-<stamp>` 镜像标签**，那是错的，而且错得有破坏性：
   本仓库自己写的 peer 发布步骤就是
   `docker compose … up -d --build db-tunnel`（见 SHARED_PEER_RUNTIME.md），
   重建会用同一套带批量转发的文件产出**新的镜像 id**——转发还在，标签却不指向它了，
   于是脚本会对一台好端端的 peer 报出「部署被中断，请从 incident-backups 恢复那三个文件」
   的拒绝，而照着做正好会把一条正常工作的批量转发从 peer 上抹掉。
   进程自己的 argv 不会被重建作废：保留了转发的重建会连转发一起重建容器，
   而**丢掉**转发的重建正是这条检查要抓的。那个 `:batch-<stamp>` 标签仍然会打
   （`check=True`，打不上就当场失败，此时还在 `up -d` 之前，回滚只是文件和标签），
   但它现在只是「这次构建产物的名字」和事后排查的抓手，**不再是判据**。

   证据不齐时**不是** `exit 0`，而是一条拒绝，并且**把两种可能都说出来**，
   因为它们要的修法正好相反：(a) 文件写完但 build / `up -d` 被打断——
   去 `incident-backups` 恢复那三个文件后重跑；(b) 文件是对的但容器没按它重建过
   （手工改过，或 build 之后 `up -d` 没跑）——用 peer 自己的发布步骤
   `up -d --build db-tunnel` 重建后再跑一次确认。脚本不替操作者在两者之间猜。
   容器没在跑、或者 `docker exec` 读不到 pid 1 的命令行，也都各自点名报出来。
   只看「键在不在」是不够的：本仓库自己的 compose 无条件声明
   `PEER_BATCH_DB_PORT: ${PEER_BATCH_DB_PORT:-}`，而 `.env.example` 里这个变量是
   注释掉的，所以在一台什么都没部署过的 peer 上 `docker compose config` 照样会
   渲染出这个键（值为空）；写完 compose 就被打断的那一次运行也是同一个形状。
   这两种情况都必须落回正常的 `matches` / `mismatch` 闸门，
   而不是被当成「成功的空操作」。
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

失败时的**自动回滚**：恢复三个配置文件、把 `:pre-batch-<stamp>` 重新指回原标签；
如果失败发生在第 7 步 `up -d` **之后**（脚本用 `recreated` 标记记录这一点），
还会**再执行一次 `up -d --no-deps --no-build db-tunnel` 把容器重建回旧镜像**，
并**重新探测一次 5432**，打印 peer 是否已经恢复。
只改标签是不够的：重打标签对已经在跑的容器没有任何作用，
所以 `wait_healthy` 超时、5433 探测失败、以及最关键的
「5432 探测失败」（探测顺序正是为了抓这一种）这三条路径，
如果不重建，lightServer 会继续用脚本刚刚判定为坏的镜像对外提供数据库，
而脚本却声称自己回滚了。

**标签这一步在两条路径上都要验，不只是重建那条。** `compose build` 已经把
`:latest` 指向新镜像了，所以即使失败发生在构建之后、`up -d` 之前（容器压根没动过），
重打标签失败也意味着这个引用还指着那个被判定为坏的构建：peer 现在还在用旧镜像，
但下一次 `up -d`、重启或开机就会**静悄悄地**从坏镜像起来。
所以只要构建跑过，脚本就会用 `docker image inspect <原标签>` 把引用**读回来**
（`docker tag` 返回 0 只说明命令执行了，不说明引用现在指向哪里），
和构建前保存的镜像 id 比对，并把 `preserved image id` / `now resolves to` /
`retag exit code` 三行都打出来。没重建但标签没回去的那条路，
打印的是醒目的 `IMAGE TAG NOT RESTORED` 加人工回滚命令，
而不是原来那句「nothing else changed on this peer」。

重建之后脚本**不相信自己**：`docker tag` 的退出码被保留，
重建出来的容器再用 `docker inspect` 读一次镜像 id，
和构建前保存的那一个**逐字比较**，两个 id 都会打印出来
（上面那三行加上 `recreated image id`）。
这是必须的一步——`up -d --no-build` 解析的是**此刻**标签指向的镜像，
所以如果重打标签失败（`:pre-batch-<stamp>` 被并发的 `docker image prune` 清掉、
daemon 报错、磁盘满），`up -d` 依然返回 0，容器却是从那个刚被判定为坏的镜像起来的。
只有两个 id 相等时才会重新探测 5432 并报告「rolled back」；
否则（重建失败、或镜像 id 对不上）一律打印
「AUTOMATIC ROLLBACK FAILED」，附上两个 id 和 retag 的退出码，
并要求人工执行下面第 3 节的命令。

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
  真实状态再动手，「拒绝」与「patch」两条路径现在由
  `quant-service/tests/test_peer_batch_tunnel_deploy.py` 真正执行：
  夹具 `quant-service/tests/fixtures/peer-ssh-tunnel-entrypoint-nc-legacy-20260917.sh`
  是 peer 现网 entrypoint 的逐字节副本（1694 字节，SHA-256 `b0aa1e02…4072c`，
  2026-09-19 只读取回），测试断言 patch 结果 `sh -n` 通过、沿用 `0.0.0.0` 绑定、
  幂等，并用内存中的渲染服务驱动 `inspect_peer_state` 的各条拒绝路径。
  **peer 文件一旦变化，夹具哈希断言和 `KNOWN_PEER_STATES` 会同时失败**——
  这是有意的：两者必须一起更新。自动回滚里的「重建容器」分支同样没有真跑过。
- 批量任务还没有对应的 kill-and-recover 验收（intraday 有
  `test-shared-tunnel-recovery.ps1`）。装好之后值得跑一次。
