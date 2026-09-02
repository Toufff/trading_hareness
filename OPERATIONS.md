# Feishu relay/workbench operations

This file used to be the complete macOS + Colima + Docker Compose runbook for
the local n8n and Feishu-adapter stack (checkout name `n8n`, container
management via `docker compose`, autostart via `launchd`). That deployment
mechanism is archived: production now runs on the Windows `G:\StockPlatform`
platform, and `feishu-adapter` starts directly via `node.exe` under the
Windows runtime supervisor rather than inside a Colima-managed container.

**For how to operate the current deployment, start with
[`docs/AGENT_HANDOFF.md`](docs/AGENT_HANDOFF.md)** (first-five-minutes health
checks, publish/rollback, evidence locations) and
[`docs/SHARED_PEER_RUNTIME.md`](docs/SHARED_PEER_RUNTIME.md) (owner/peer
topology, Windows runtime observability, `get-stock-runtime-status.ps1`).

The Feishu relay/workbench *behavior* — webhook shapes, media chunking
protocol, group-relay forwarding rules, the manual relay page at `/relay`,
the workbench at `/workbench`, permission requirements, and troubleshooting
for the adapter itself — is unchanged from the original macOS-era
description and has been archived verbatim (with a few dangling script/path
references annotated and corrected) at
[`docs/legacy/MACOS_COLIMA_ERA.md`](docs/legacy/MACOS_COLIMA_ERA.md). Read
that page for:

- the four production Feishu webhook entry points and their payload shapes;
- the media chunking limits and idempotency rules;
- the external group-relay forwarder (`#anqiang`/`#liwei`/`#quanneng`/`#xiaolan`/`#liuzi`) and the analyst workbench;
- the manual delivery page (`/relay`) and its clipboard-hotkey helper;
- the macOS-only companion tooling (WeChat image/text watchers) — flagged
  there as not re-verified against the Windows migration.

Quant API endpoints, Tushare/AKShare provider configuration and the n8n
close-of-day scheduling workflows are documented in
[`DEPLOYMENT.md`](DEPLOYMENT.md).
