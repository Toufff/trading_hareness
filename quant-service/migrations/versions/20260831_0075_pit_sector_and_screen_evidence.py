"""Make sector membership as-known-at and retain full post-close screen evidence.

Revision ID: 20260831_0075
Revises: 20260828_0074
"""

from alembic import op


revision = "20260831_0075"
down_revision = "20260828_0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing 1900-01-01 rows remain source-auditable, but their start was a
    # synthetic implementation default.  Mark them as legacy rather than
    # retconning a precise historical membership date that no provider gave.
    op.execute("ALTER TABLE quant.sector_membership_history ADD COLUMN IF NOT EXISTS known_at timestamptz")
    op.execute("ALTER TABLE quant.sector_membership_history ADD COLUMN IF NOT EXISTS effective_from_basis text")
    op.execute("ALTER TABLE quant.sector_membership_history ADD COLUMN IF NOT EXISTS effective_to_basis text")
    op.execute("""
        UPDATE quant.sector_membership_history
           SET known_at=coalesce(known_at,available_at),
               effective_from_basis=coalesce(effective_from_basis,'legacy_unbounded'),
               effective_to_basis=coalesce(effective_to_basis,'legacy_unbounded')
    """)
    op.execute("ALTER TABLE quant.sector_membership_history ALTER COLUMN known_at SET NOT NULL")
    op.execute("ALTER TABLE quant.sector_membership_history ALTER COLUMN known_at SET DEFAULT now()")
    op.execute("ALTER TABLE quant.sector_membership_history ALTER COLUMN effective_from_basis SET NOT NULL")
    op.execute("ALTER TABLE quant.sector_membership_history ALTER COLUMN effective_from_basis SET DEFAULT 'legacy_unbounded'")
    op.execute("ALTER TABLE quant.sector_membership_history ALTER COLUMN effective_to_basis SET NOT NULL")
    op.execute("ALTER TABLE quant.sector_membership_history ALTER COLUMN effective_to_basis SET DEFAULT 'legacy_unbounded'")
    op.execute("""
        CREATE INDEX IF NOT EXISTS sector_membership_pit_provenance_idx
            ON quant.sector_membership_history(taxonomy_key, effective_from_basis, known_at, effective_from DESC)
    """)

    # A candidate table alone makes the rejected/no-signal universe disappear.
    # This compact projection retains the deterministic decision for every
    # symbol that entered a completed post-close screen, without duplicating
    # the underlying daily bars.
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.post_close_strategy_screen_observations (
            run_id uuid NOT NULL REFERENCES quant.post_close_strategy_runs(run_id) ON DELETE CASCADE,
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            name text,
            screen_state text NOT NULL CHECK (screen_state IN ('candidate','rejected','insufficient_history')),
            candidate_type text,
            score numeric,
            reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
            structure jsonb NOT NULL DEFAULT '{}'::jsonb,
            board_context jsonb NOT NULL DEFAULT '{}'::jsonb,
            source_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(run_id,symbol)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS post_close_screen_observations_state_idx
            ON quant.post_close_strategy_screen_observations(run_id,screen_state,candidate_type)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.post_close_strategy_screen_observations")
    op.execute("DROP INDEX IF EXISTS quant.sector_membership_pit_provenance_idx")
    op.execute("ALTER TABLE quant.sector_membership_history DROP COLUMN IF EXISTS effective_to_basis")
    op.execute("ALTER TABLE quant.sector_membership_history DROP COLUMN IF EXISTS effective_from_basis")
    op.execute("ALTER TABLE quant.sector_membership_history DROP COLUMN IF EXISTS known_at")
