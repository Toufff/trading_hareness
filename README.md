# Local n8n (Colima)

This instance is deliberately local-only: n8n listens on `127.0.0.1:5678` and PostgreSQL has no host port. The state is stored in the Colima-managed Docker volumes `n8n_n8n_data` and `n8n_postgres_data`.

For the verified current workflow, service topology, automatic startup behavior, recovery commands, and troubleshooting, see [OPERATIONS.md](OPERATIONS.md).

The local research service and the future server deployment path are documented in [DEPLOYMENT.md](DEPLOYMENT.md). The complete analyst-channel quant research design and phased acceptance plan are in [docs/QUANT_RESEARCH_IMPLEMENTATION_PLAN.md](docs/QUANT_RESEARCH_IMPLEMENTATION_PLAN.md). The default Compose stack remains local-only; the separate server composition exposes only TLS reverse-proxy endpoints.

The Windows `stock-brain` cutover, facts-only import boundary, actual-portfolio
contracts and personal decision dashboard are documented in
[docs/STOCK_BRAIN_MIGRATION.md](docs/STOCK_BRAIN_MIGRATION.md). The migration
never exposes a broker order path and does not import legacy action-card plans.

The optional collaborator runtime—G-drive PostgreSQL authority, loopback-only
SSH relay, rootless Docker boundary, Longhu licensed-read gateway, candidate
migration and rollback—is documented in
[docs/SHARED_PEER_RUNTIME.md](docs/SHARED_PEER_RUNTIME.md).

## Windows stock platform runtime

The migrated stock platform is code in this repository but keeps all large,
authoritative state on the local 12 TB data disk:

```text
G:\StockPlatform\
  config\runtime.env
  data\postgresql16\
  data\imports\
  data\raw\
  runtime\postgresql-16.15\
```

PostgreSQL listens on `127.0.0.1:55432` only.  Do not move its cluster, raw
evidence or bulk imports into the checkout, a Git history or the lightServer.
The lightServer may host a static UI and authenticated reverse proxy, while the
G-drive database remains authoritative.

The local runtime is kept healthy by `scripts/windows/start-stock-dashboard.ps1`.
It rejects non-`G:` platform roots, starts the database/API/adapter without visible
terminals, and maintains a loopback-only reverse tunnel to LightServer. The
server hosts only versioned frontend assets and an HTTPS reverse proxy; it never
stores the authoritative portfolio or market database.

The quant API defaults to `http://127.0.0.1:5681`.  The personal decision page
separates the settled market review, exact CITIC holdings, qualified conditional
buys and the human-readable research audit.  No route places, modifies or
cancels broker orders.  See
[docs/STOCK_BRAIN_MIGRATION.md](docs/STOCK_BRAIN_MIGRATION.md) for the contracts,
endpoints and live cutover acceptance criteria.

## Start

```bash
cd /Users/papa/codebase/n8n
colima status
docker compose pull
docker compose up -d
docker compose ps
```

Open [http://localhost:5678](http://localhost:5678) and create the n8n owner account.

## Operate

```bash
docker compose logs -f n8n
docker compose stop
docker compose start
docker compose pull && docker compose up -d
```

The service also starts automatically after you log into macOS. Colima itself is managed by Homebrew's `homebrew.mxcl.colima` LaunchAgent; `com.papa.n8n-compose` waits for its Docker daemon, then reconciles this Compose project. Inspect the user service or its log with:

```bash
launchctl print gui/$(id -u)/com.papa.n8n-compose
tail -f ~/Library/Logs/n8n-compose-launchd.log
```

Do not delete the Docker volumes or `.env`: together they preserve workflows, execution history, user data, and the credential encryption key.

## Adding Feishu later

The Compose project includes a Feishu long-connection adapter. It has no public host port and sends `im.message.receive_v1` events to `http://n8n:5678/webhook/feishu-market-import` over the internal Compose network.

The adapter also exposes a local-only real-time monitor at [http://localhost:5680](http://localhost:5680). It displays the most recent 200 message events plus their n8n result and target-import queue status from the current adapter process; the in-memory list resets when the adapter restarts.

The relay and monitor UI are built as a Vue 3 + Vite + TypeScript SPA and served by the adapter. The relay uses multipart upload with browser progress and cancellation; the legacy inline HTML can be restored with `FRONTEND_MODE=legacy` if needed. The local API remains unchanged (`/manual-relay`, `/events`, `/jobs`, `/metrics`, and `/api/config`).

Durable local job state is stored in PostgreSQL. Repeated Feishu events and repeated media SHA-256 values are classified locally before any remote request. Failed jobs remain inspectable through `/api/jobs/:job_id`; an explicit `POST /api/jobs/:job_id/retry` is required to retry.

生产入口已拆分为文本 `feishu-market-text`、媒体分片 `feishu-media-part`、媒体完成 `feishu-media-finalize` 和上传状态核验 `feishu-media-state` 四个 Webhook。文本为 JSON；图片、音频、视频由适配器按 8 MiB multipart 分片逐片发送。每个媒体会话只创建一次，多个媒体在最后一次 finalize 后才 submit；手动重试会先核验远端已收分片，只补传缺失部分。

For image, audio, or video forwarding, grant and publish the Feishu app's application permission `im:message:readonly` (or a broader listed `im:message` permission). The adapter downloads the message resource through Feishu's official `im/v1/messages/:message_id/resources/:file_key` API; it does not read the desktop client, scrape a window, or export cookies.

The adapter needs only the App ID and App Secret in `.env`; no public callback URL, Verification Token, or Encrypt Key is needed in long-connection mode. Check the connection with:

```bash
docker compose logs -f feishu-adapter
```

If this instance is later made public through a reverse proxy, change `N8N_HOST`, `N8N_PROTOCOL`, `N8N_EDITOR_BASE_URL`, and `WEBHOOK_URL`, and restore secure cookies.
