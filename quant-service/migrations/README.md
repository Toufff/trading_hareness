# Quant schema migrations

`20260811_0001` is a no-op baseline for the schema already created by legacy
startup DDL. Existing environments are stamped to it only after a verified
PostgreSQL backup. `20260811_0002` adds durable `quant.runtime_leases` used by
the post-close refresh so separate service processes cannot run that ordered
pipeline concurrently. New DDL must use a revision in `versions/`.

The production image runs `alembic upgrade head` before `uvicorn`. It holds a
PostgreSQL advisory lock while migrating, so concurrent service starts cannot
apply the same revision simultaneously. The lock wait is bounded by
`QUANT_MIGRATION_LOCK_TIMEOUT_SECONDS` (default: 60). This startup step is
idempotent when the database is already at the Alembic head and never invokes
market-data providers.
