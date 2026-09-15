# 2026-09-14：远端数据库连接恢复后仍持续 500

后续还发现并修复了同机公网绕行造成的真实间歇超时，见
[PEER_PRIVATE_TUNNEL_20260914.md](PEER_PRIVATE_TUNNEL_20260914.md)。本页保留前一轮验收范围，
不能用前一轮空闲/断线测试代替后续持续负载下的网络验收。

## 已确认原因与最小修复

Windows 数据库恢复、15432 转发正常后，lightServer 的两个量化进程仍处于
`pool_size=0 / available=0 / waiting=64`。在故障容器内，新建连接可以执行
`SELECT 1`，但进程内旧异步池拒绝所有请求。不是仍然断网，也没有证据证明数据库锁死。

psycopg_pool 3.2.6 的重连预算耗尽后，连接数降为零；过期等待项占满队列，
`getconn()` 在触发扩容前就拒绝请求。修复只使用公开的 `pool.check()`：

- 每次异步事务取得连接前，若连接数为零，要求池重新补建连接。
- 单进程锁保护，最多每 5 秒一次，调用预算 1 秒。
- 普通忙碌池（连接数大于零）不处理；已关闭的池不重开。
- 不更换活跃连接池，不自动重放 SQL/交易，不新增守护进程。
- `/health` 先对业务实际使用的异步池做 `SELECT 1`，失败返回 503；
  不再只检测健康的同步池而宣称整个数据库依赖健康。

开发源码：`quant-service/app/empty_pool_recovery.py`、`database.py`、
`routers/system_control.py`；`main.py` 仅增加依赖注入。

## 部署范围与身份

- 主机：lightServer，47.110.79.189，rootless Docker 用户 stockpeer。
- 15682：`trading-hareness-peer-quant-research-1`，盘中运行模式。
- 15683：`trading-hareness-peer-quant-research-scheduler-1`，研究运行模式。
- 基线镜像：`sha256:0e0a6a631bdbb5dfa4948ed08de03b19a8ac885bc06fbc5ae5e25c608888ee04`。
- 部署标签：`trading-hareness-peer-quant-research:pool-recovery-20260914`。
- 部署镜像：`sha256:f3f62fd1b54b3374d5c78e441abc0157aba3f1410ee3a2772ddfc23c5d5f495f`。
- 2026-09-14 17:37 CST 部署；保留既有环境、网络、数据卷、端口。
- 发现盘中模式原来只由临时环境传入；已把当前实际值
  `PEER_BACKGROUND_TASKS_ENABLED=true` / `PEER_RUNTIME_PROFILE=intraday_edge`
  固化到服务器原有私有 `.env`，防止重建时退回停用状态。
- 原有 `compose.intraday-owner.yaml` 两个服务均固定上述镜像，原启动命令无需额外 override。
- 没有数据库迁移，没有改变上游数据源、权限或 API 地址。
- 没有发布 Windows 本机的新版本：本次故障进程在服务器，Windows 本机 API/PG 不需要重启。

这是从远端实际运行镜像生成的最小兼容补丁，不是把当前脏工作区覆盖到远端。
`build-empty-pool-hotfix.py` 使用唯一源码锚点，版本不符立即中止；它是本次事件工具，
不要在已修复的容器上重复运行。后续整版升级必须保留源码修复和回归测试，
不要对服务器旧源码直接 `compose up --build`，那会重新构建未修复的旧版本。

## 真实验收结果（不等同于整个交易系统验收）

1. 本地后端测试：1852 项运行，85 项按原有条件跳过，其余通过。
2. 实际 psycopg_pool/PostgreSQL 故障复现：三轮构造 **零连接、64 个过期等待项**；
   恢复目标后旧行为仍抛 TooManyRequests；修复后同一池自行恢复 `SELECT 1`，
   不替换池、不重启进程。使用隔离池，不向生产表写数据。
3. 已部署服务实测：停止远端 `db-tunnel`，两服务 `/health` 均返回 503；
   恢复隧道后约 11.22 秒内恢复。独立 20 秒恢复进程与 finally 恢复保护均有配置。
4. 两个量化容器的 ID、PID、RestartCount 在断线测试前后完全一致。
5. 两服务各自 `/health`、`/api/v1/providers/health`、`/api/v1/strategy/health`、
   `/api/v1/intraday/services/status`、`/api/v1/automation/runs?limit=1`
   共 10 项恢复 HTTP 200。
6. 后续检查：异步池分别 4/3 个可用连接、等待数为零；7 个盘中循环与 4 个研究循环
   心跳继续更新、last_error 为空。收盘后的循环存活不代表实时采集内容已重新验收。

证据：`G:\StockPlatform\peer\acceptance\pool-recovery-20260914\`，包含
`build-evidence.json`、`real-pool-regression.json`、`deployed-outage-recovery.json`。
远端副本：`/home/stockpeer/.local/share/trading-hareness/hotfixes/pool-recovery-20260914/`。

## 复验与回滚

无中断回归：在已修复镜像中，以现有 PG 环境运行
`scripts/shared-peer/test-empty-pool-recovery.py`；只建立独立测试池，执行 SELECT 1。

`verify-peer-pool-recovery.py` **会实际中断远端共享隧道**，只用于明确授权的维护窗口，
不得配置为定时任务。它验证故障时 503、恢复时 200、应用进程不重启。

私有配置备份（含凭据，不得入 Git 或给公开链接）：
`/home/stockpeer/.local/share/trading-hareness/incident-backups/20260914T173727-pool/`。
旧镜像已保留为 `trading-hareness-peer-quant-research:before-pool-recovery-20260914`。
若补丁有回归，只把 compose 中两个 image 改为该旧标签，再用原有两份 compose
执行 `up -d --no-deps --no-build quant-research quant-research-scheduler`。
保留已固化的盘中模式，禁止删除数据卷；回滚会重新暴露旧连接池缺陷。

## 未在本次宣称完成的范围

协作者 47.114.113.152 的 edge 容器旧 URL（18110）、Mac 旧 LaunchAgent、n8n 413、
媒体 workflow、Tushare 代理，以及完整策略产物属于其他链路。
本机未取得对该 edge 主机已核实的 SSH 接入，因此不能声称它们已修复。
协作者应先从自己的实际入口复验 15682 的上述业务接口；不能用 /health 单项
代替业务验收，也不能拿 Windows 的 5681 正常证明 Mac 的 5681 正常。
