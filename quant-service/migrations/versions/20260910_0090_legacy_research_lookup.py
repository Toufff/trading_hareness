"""Index imported per-stock research without scanning million-row JSON history.

Revision ID: 20260910_0090
Revises: 20260902_0089
"""
from alembic import op

revision = '20260910_0090'
down_revision = '20260902_0089'
branch_labels = None
depends_on = None


def upgrade():
    for field in ('security_code','code'):
        op.execute(f"""CREATE INDEX IF NOT EXISTS legacy_research_{field}_idx
            ON quant.legacy_source_records ((payload->>'{field}'),available_at DESC)
            WHERE source_table IN ('research_runs','research_gates','investment_theses',
                'pool_memberships','short_term_scan_results','trade_setups','trade_setup_events')""")


def downgrade():
    op.execute('DROP INDEX IF EXISTS quant.legacy_research_security_code_idx')
    op.execute('DROP INDEX IF EXISTS quant.legacy_research_code_idx')
