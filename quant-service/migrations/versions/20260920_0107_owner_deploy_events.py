"""Announce owner deployments on the one channel that survives them.

Revision ID: 20260920_0107
Revises: 20260919_0106

A release restarts the owner HTTP surface, and sometimes the shared-peer
tunnel.  Measured on 2026-09-20 across three publishes: ``quant-api`` was down
9.3 s each time, the shared tunnel 5-7 s on two of the three, and PostgreSQL
itself did not restart at all.  Fifteen seconds is survivable -- but only by a
consumer that knows it is a deployment and not a fault, and the external peer
has no way to tell the two apart.  It cannot be told over HTTP either, because
HTTP is exactly what goes away.

The database is the channel that stays up, so the announcement goes here.  The
publish script writes ``starting`` *before* it touches anything -- including
before a tunnel reinstall -- so the row is already readable when the consumer's
connection drops, and writes ``completed`` or ``failed`` once the health gate
has decided.  ``surfaces`` says which paths this particular deploy will
interrupt, because the tunnel gate's answer is known before the stop and a
deploy that spares the tunnel costs the consumer nothing at all.

``deploy_id`` groups the phases of one deploy; the table is append-only so a
crashed publish leaves its ``starting`` row behind rather than a lie.

SELECT is granted explicitly to ``stock_peer``.  The role does inherit it
through ``quant_app`` today, but an announcement channel that depends on an
implicit membership is the kind of thing that silently stops working.
"""

from alembic import op


revision = "20260920_0107"
down_revision = "20260919_0106"
branch_labels = None
depends_on = None

CONSUMER_ROLE = "stock_peer"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.owner_deploy_events (
            event_id         bigserial PRIMARY KEY,
            deploy_id        text        NOT NULL,
            phase            text        NOT NULL,
            release_id       text        NOT NULL,
            git_sha          text,
            surfaces         jsonb       NOT NULL DEFAULT '{}'::jsonb,
            expected_seconds integer,
            note             text,
            recorded_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT owner_deploy_events_phase_check
                CHECK (phase IN ('starting', 'completed', 'failed'))
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS owner_deploy_events_recorded_idx
            ON quant.owner_deploy_events (recorded_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS owner_deploy_events_deploy_idx
            ON quant.owner_deploy_events (deploy_id, event_id)
    """)
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{CONSUMER_ROLE}') THEN
                EXECUTE 'GRANT SELECT ON quant.owner_deploy_events TO {CONSUMER_ROLE}';
            END IF;
        END
        $$
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.owner_deploy_events")
