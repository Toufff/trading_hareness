"""Record leader-flow outcomes net of a round trip, not only gross.

Every settled return was gross. At the size of edge these modes produce that
is not a detail: on 2026-08-27 ``supplement_rotation`` settled at +0.36%
against a +0.53% market median, and a 0.26% round trip is the difference
between a marginal positive and a clear negative. A scorecard reporting only
gross keeps recommending strategies that lose money after costs.

The applied rate is stored per row rather than assumed at read time, so a
later change to the commission, stamp or slippage assumption cannot silently
restate what earlier sessions were judged against.

Revision ID: 20260828_0073
Revises: 20260827_0072
"""

from alembic import op


revision = "20260828_0073"
down_revision = "20260827_0072"
branch_labels = None
depends_on = None

COLUMNS = ("round_trip_cost_pct", "net_session_return_pct", "net_next_open_to_close_pct")


def upgrade() -> None:
    for column in COLUMNS:
        op.execute(
            f"ALTER TABLE quant.xiaojie_leader_flow_outcomes "
            f"ADD COLUMN IF NOT EXISTS {column} numeric"
        )
    # Existing rows keep their gross columns and are left with null net values
    # rather than being back-filled at today's rate: they were settled under no
    # cost assumption at all, and inventing one would misdate the decision.
    # Re-running settlement for those dates fills them in properly.


def downgrade() -> None:
    for column in COLUMNS:
        op.execute(
            f"ALTER TABLE quant.xiaojie_leader_flow_outcomes DROP COLUMN IF EXISTS {column}"
        )
