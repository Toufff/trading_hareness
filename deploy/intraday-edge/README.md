# Intraday edge deployment

This role is the single writer for live polling and Feishu research alerts.
It runs the `intraday_edge` background profile on a loopback-only FastAPI
process and keeps the local workstation in the complementary `research`
profile. It never submits orders.

The target host uses its existing PostgreSQL instance and a dedicated
`quant_intraday_edge` database. The service is bounded by systemd to 1.4 GB
memory; source, virtualenv, database and logs should remain below 5 GB. The
daily timer refreshes only the current trade date after the A-share close.
The materialization timer builds the next-session ten-day shadow pool at
18:55/19:15/19:35 CST; repeated calls are idempotent and remain research-only.

The workstation pulls retained evidence through a dedicated restricted SSH
key and `scripts/pull-intraday-edge-evidence.sh`. The forced remote command can
only emit the allowlisted JSONL evidence tables. New edge releases append a
profile-gated change journal and the workstation persists its last imported
sequence; each pull replays a bounded tail before advancing that sequence, so
normal short transaction/connection interruptions are idempotently recovered.
Older releases fall back once to a bounded 30-day snapshot bootstrap. Imports
are transactional, upsert mutable evidence, and never copy runtime leases,
alert deliveries, recommendations, credentials or order state. A local launch
agent may call the script every 15 minutes; when the Mac is off, the remote
database simply retains the evidence for the next pull.

The puller records its latest local attempt separately from the evidence
cursor. A failed pull therefore appears as a visible warning with its last
error and last success time; it does not change the edge collector's ownership
or make a stale snapshot look healthy.
`edge_export_grants.sql` grants that account SELECT on the same explicit table
set only; it does not receive default access to future schema additions.

## Market-session acceptance

Run `scripts/verify-intraday-edge-live-session.sh` during an SSE continuous
auction session (09:30–11:30 or 13:00–15:00, Asia/Shanghai). It is read-only
and checks the remote edge's release identity, `intraday_edge` runtime profile,
and every currently expected market-data loop's fresh observation, age budget,
and error state. It exits with code `3` outside that session rather than
mistaking an intentional standby state for a passing live acceptance. Use
`--allow-standby` for a control-plane-only off-session check.

Required secret environment values are installed directly into
`/etc/quant-intraday-edge.env` with mode `0640`; they are not stored here.

## Release identity

The service runs one committed source release through the `current` symlink.
`scripts/deploy-intraday-edge-release.sh` retains each revision under
`/opt/quant-intraday-edge/releases/<git-sha>` and writes only non-secret build
provenance to `/etc/quant-intraday-edge.release.env`. `/health.build` then
reports the deployed Git SHA, release label, and build timestamp. See
[`RELEASE.md`](RELEASE.md) for the fenced deployment command.
