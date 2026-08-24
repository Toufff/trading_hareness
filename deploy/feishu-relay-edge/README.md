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
forwarding. The remote `relay.env` is mode 0640 and is not stored in git.

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
read-only and fails on workflow drift.

`scripts/deploy-feishu-relay-edge-release.sh <git-sha> <release-label>` is a
dry run by default. `--apply` pulls the immutable image, updates only the
non-secret release keys in remote runtime configuration, and recreates the
adapter while retaining its durable PostgreSQL relay ledger and media volume.

## Deterministic emergency failover to the workstation

From the repository root, run:

```bash
bash scripts/failover-feishu-relay-to-local.sh
```

The script deliberately fails closed. It first disables both remote pollers,
copies the remote durable ledger (including the `source_message_id` primary-key
dedupe rows, source cursors, OAuth refresh state and media retry state) into
the local database in a single transaction, then starts the local pollers.
Consequently a source message already marked `sent` remotely cannot be claimed
or sent again locally. If the server cannot be reached to take that snapshot,
the script leaves local polling disabled instead of risking duplicate delivery.
