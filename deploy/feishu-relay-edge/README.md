# Always-on Feishu relay edge

This deployment owns the user-OAuth group-history poller, durable relay ledger,
media retry state and the four inbound n8n webhook workflows. It binds n8n
(`127.0.0.1:5678`) and the adapter dashboard (`127.0.0.1:18300`) only to the
server loopback interface. PostgreSQL is the host's existing local instance;
the deployment uses a dedicated `n8n_relay` database and role.

Cutover order is deliberate: start the remote adapter with both pollers
disabled, restore the OAuth/ledger state and import the webhook-only workflows,
disable the local pollers, then enable the remote pollers. This preserves the
source cursors and message-ID dedupe boundary while avoiding duplicate group
forwarding. `FEISHU_RELAY_WRITER_ID` is a named writer generation recorded in
the relay ledger; it is an operational fence after a copied ledger, not a
cross-host lock (the two hosts have separate PostgreSQL instances). The remote
`relay.env` is mode 0640 and is not stored in git.

The workstation keeps a dedicated `feishu_relay_edge_ed25519` key under its
private `.ssh` directory. Both handoff scripts use it by default; another
operator can override it through `RELAY_EDGE_SSH_KEY` without placing a key in
this repository.

The summary-listener ingestion path is two phase: it durably stores the source
message, parsed content and local media before writing one
`ingestion_delivery_outbox` row. A leased worker then calls n8n. A transport
outage therefore retains the original `message_id` for retry instead of being
mistaken for a completed delivery. The failover snapshot includes this outbox
and its media files.

Only these workflows are imported: text aggregation plus media-part,
media-finalize and media-state callbacks. Local scheduled market workflows are
not copied, so this relay deployment cannot duplicate local research schedules.

## Versioned release

The adapter source is built by the GitHub `edge-*` release workflow into an
immutable GHCR image. The remote runtime selects that image through the ignored
`FEISHU_ADAPTER_IMAGE` value in `runtime.env`; it does not build from an
uncommitted server directory. Both `/health` and the quant edge health endpoint
publish a non-secret `build` object with `git_sha`, `release`, and
`build_created_at`.

Use `scripts/export-edge-relay-workflows.sh` to refresh the redacted canonical
workflow JSON under `workflows/edge-relay/`, then use
`scripts/verify-edge-relay-workflows.sh` before a deployment. The latter is
read-only and fails on workflow drift. Both commands use the dedicated
`feishu_relay_edge_ed25519` key by default (or `RELAY_EDGE_SSH_KEY` when an
operator supplies a different key), never an interactive password prompt.
Version IDs, execution counters, timestamps and n8n static runtime state are
deliberately ignored by the drift comparison; node graphs, connections,
settings and callback URLs are not.

`scripts/deploy-feishu-relay-edge-release.sh <git-sha> <release-label>` is a
dry run by default. `--apply` pulls the immutable image, updates only the
non-secret release keys in remote runtime configuration, and recreates the
adapter while retaining its durable PostgreSQL relay ledger and media volume.

For a committed webhook workflow revision, use:

```bash
bash scripts/deploy-edge-relay-workflows.sh <git-sha> --apply
```

It exports a retained rollback copy on the edge, renders the redacted remote
archive base URL there, stops n8n before changing its persisted graph, publishes
all four workflow versions in one transaction, and then verifies the live
export against the committed source. It refuses an uncommitted worktree or a
workflow containing the obsolete Docker-only callback address.

The remote archive control plane has one Gunicorn worker, so a stuck upload can
otherwise exhaust all of its request threads. Install the version-controlled
watchdog after a release with:

```bash
bash scripts/install-edge-import-watchdog.sh --apply
```

Every minute it expects the deliberately unsupported `GET /api/v1/imports/batches`
to return `401`, `403`, or `405`; a timeout or unexpected response restarts only
`stock-reports-import.service`. It neither creates an import batch nor touches
the durable relay ledger.

## Deterministic emergency failover to the workstation

From the repository root, run:

```bash
bash scripts/failover-feishu-relay-to-local.sh
```

The script deliberately fails closed. It first disables both remote pollers,
copies the remote durable ledger (including the `source_message_id` primary-key
dedupe rows, source cursors, OAuth refresh state, media retry state and writer
generation) into the local database in a single transaction, promotes the
local writer generation, then starts the local pollers.
Consequently a source message already marked `sent` remotely cannot be claimed
or sent again locally. If the server cannot be reached to take that snapshot,
the script leaves local polling disabled instead of risking duplicate delivery.

## Deliberate failback to the edge

After the edge is healthy again, return ownership with:

```bash
bash scripts/failback-feishu-relay-to-remote.sh
```

It fences local polling first, copies the full ledger, outbox and retry-media
back to the edge, increments the edge writer generation, then enables only the
edge pollers. It is intentionally not automatic: a network partition must not
be allowed to create two group-history writers.
