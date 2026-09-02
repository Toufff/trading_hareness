"""Methodology-version stamping and pre-overwrite history for outcome tables.

Revision ID: 20260902_0086
Revises: 20260902_0085

Several outcome tables upsert with ``ON CONFLICT ... DO UPDATE`` keyed on the
claim/candidate identity alone, so a re-run under a changed methodology (a
provider unit fix, a T+1 lag fix, a delisted-symbol handling fix, ...)
silently overwrites the only record of the prior computation: the result is
not reproducible after the fact.

A primary-key change to make every table natively append-only (add
``methodology_version`` to the key) is the ideal fix, but four tables are
read by code and views outside this work package (async read repositories,
main.py routes, the frontend); changing their primary keys here risks
breaking those readers out of band.  The safer, minimal fix applied instead:

  * add a ``methodology_version`` column (defaulted to the version string the
    settlement module used before this change, so existing rows are labelled
    honestly rather than silently defaulted to a *new* version) and a
    ``bars_snapshot_hash`` column (nullable; populated going forward with a
    hash of the priced bars an outcome was computed from, so a later
    provider correction that changes the same bars is visible even though the
    row's identity did not change);
  * add one ``<table>_history`` table per outcome table that receives a full
    JSONB snapshot of the row being overwritten, immediately before the
    ``ON CONFLICT ... DO UPDATE`` runs.  The live table therefore always
    holds the latest computation (so no reader has to change), and the
    history table holds every value it ever superseded.

``quant.analyst_opinion_outcomes`` already carries ``methodology_version`` in
its own primary key (append-only by construction); it only needs the history
table, because ``analyst_expert_research.rebuild_analyst_opinions`` hard
``DELETE``s an opinion's outcomes when the opinion's source claims change,
which is switched to archive-then-delete.
"""

from alembic import op


revision = "20260902_0086"
down_revision = "20260902_0085"
branch_labels = None
depends_on = None


# table, default methodology_version label, key columns copied onto the
# history row for indexed lookups (all history tables also keep a full JSONB
# snapshot so the key-column list does not need to be exhaustive).
_VERSIONED_TABLES = (
    ("outcomes", "outcome-recomputation-v1", ("claim_id", "recommendation_run_id", "symbol", "entry_date", "horizon_days")),
    ("post_close_strategy_candidate_outcomes", "post-close-candidate-outcome-v1", ("run_id", "symbol")),
    ("ten_day_leader_rotation_candidate_outcomes", "post-close-candidate-outcome-v1", ("run_id", "symbol")),
    ("intraday_signal_outcomes", "intraday-outcome-settlement-v1", ("signal_event_id", "horizon_key")),
    ("xiaojie_leader_flow_outcomes", "xiaojie-outcome-settlement-v1", ("trading_date", "symbol", "mode")),
)


def upgrade() -> None:
    for table, default_version, _keys in _VERSIONED_TABLES:
        op.execute(f"""
            ALTER TABLE quant.{table}
                ADD COLUMN IF NOT EXISTS methodology_version text NOT NULL DEFAULT '{default_version}',
                ADD COLUMN IF NOT EXISTS bars_snapshot_hash text
        """)
        op.execute(f"""
            CREATE TABLE IF NOT EXISTS quant.{table}_history (
                history_id bigserial PRIMARY KEY,
                claim_id uuid,
                recommendation_run_id uuid,
                run_id uuid,
                signal_event_id uuid,
                symbol text,
                entry_date date,
                horizon_days integer,
                horizon_key text,
                trading_date date,
                mode text,
                methodology_version text,
                bars_snapshot_hash text,
                old_row jsonb NOT NULL,
                archived_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        op.execute(f"""
            CREATE INDEX IF NOT EXISTS {table}_history_archived_idx
                ON quant.{table}_history(archived_at DESC)
        """)

    op.execute("ALTER TABLE quant.analyst_opinion_outcomes ADD COLUMN IF NOT EXISTS bars_snapshot_hash text")
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_opinion_outcomes_history (
            history_id bigserial PRIMARY KEY,
            opinion_id uuid,
            horizon_days integer,
            methodology_version text,
            old_row jsonb NOT NULL,
            archived_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS analyst_opinion_outcomes_history_opinion_idx
            ON quant.analyst_opinion_outcomes_history(opinion_id, archived_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.analyst_opinion_outcomes_history")
    op.execute("ALTER TABLE quant.analyst_opinion_outcomes DROP COLUMN IF EXISTS bars_snapshot_hash")
    for table, _default_version, _keys in reversed(_VERSIONED_TABLES):
        op.execute(f"DROP TABLE IF EXISTS quant.{table}_history")
        op.execute(f"""
            ALTER TABLE quant.{table}
                DROP COLUMN IF EXISTS methodology_version,
                DROP COLUMN IF EXISTS bars_snapshot_hash
        """)
