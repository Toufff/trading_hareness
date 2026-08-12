# Local n8n (Colima)

This instance is deliberately local-only: n8n listens on `127.0.0.1:5678` and PostgreSQL has no host port. The state is stored in the Colima-managed Docker volumes `n8n_n8n_data` and `n8n_postgres_data`.

For the verified current workflow, service topology, automatic startup behavior, recovery commands, and troubleshooting, see [OPERATIONS.md](OPERATIONS.md).

The local research service and the future server deployment path are documented in [DEPLOYMENT.md](DEPLOYMENT.md). The complete analyst-channel quant research design and phased acceptance plan are in [docs/QUANT_RESEARCH_IMPLEMENTATION_PLAN.md](docs/QUANT_RESEARCH_IMPLEMENTATION_PLAN.md). The default Compose stack remains local-only; the separate server composition exposes only TLS reverse-proxy endpoints.

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
