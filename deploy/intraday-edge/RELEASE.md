# Reproducible edge releases

The edge uses the same committed source revision as the workstation. Runtime
ownership differs only through `QUANT_RUNTIME_PROFILE=intraday_edge` and secret
environment files outside Git.

Build and release provenance is exposed by `/health.build`:

- `git_sha`
- `release`
- `build_created_at`

Use `scripts/deploy-intraday-edge-release.sh <git-sha-or-tag> <release-label>`
for a read-only plan. Add `--apply` only after the revision is committed,
reviewed, and pushed. The script retains source under
`/opt/quant-intraday-edge/releases/<sha>` and atomically updates the `current`
symlink only after the source and dependency checks pass. It does not remove
older releases.

Systemd runs `/opt/quant-intraday-edge/current`; the non-secret release fields
live in `/etc/quant-intraday-edge.release.env`, while credentials stay in the
existing `/etc/quant-intraday-edge.env`.
