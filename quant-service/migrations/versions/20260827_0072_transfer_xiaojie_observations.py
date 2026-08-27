"""Carry leader-flow observations from the edge to the research workstation.

The strategy runs on the edge and writes its observations there, but the table
was never added to the evidence transfer, so nothing reached the workstation
where outcome settlement runs. Accumulating for a week would have produced a
full record on the collector and an empty one everywhere it is analysed.

Revision ID: 20260827_0072
Revises: 20260827_0071
"""

from alembic import op


revision = "20260827_0072"
down_revision = "20260827_0071"
branch_labels = None
depends_on = None

TABLE = "xiaojie_leader_flow_observations"
TRIGGER = "capture_edge_evidence_xiaojie_leader_flow_observations"
KEYS = ("trading_date", "symbol", "mode")


def upgrade() -> None:
    quoted_keys = ", ".join(f"'{key}'" for key in KEYS)
    # The journal function already refuses to record on a non-edge runtime, so
    # importing on the workstation cannot produce an echo.
    op.execute(f"""
        DROP TRIGGER IF EXISTS {TRIGGER} ON quant.{TABLE};
        CREATE TRIGGER {TRIGGER}
        AFTER INSERT OR UPDATE ON quant.{TABLE}
        FOR EACH ROW EXECUTE FUNCTION quant.capture_edge_evidence_change({quoted_keys});
    """)
    # The export role is deliberately allowlisted table by table.
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quant_edge_export') THEN
                EXECUTE 'GRANT SELECT ON TABLE quant.{TABLE} TO quant_edge_export';
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON quant.{TABLE}")
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quant_edge_export') THEN
                EXECUTE 'REVOKE SELECT ON TABLE quant.{TABLE} FROM quant_edge_export';
            END IF;
        END $$;
    """)
