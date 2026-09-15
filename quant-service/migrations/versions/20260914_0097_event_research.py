"""Point-in-time source documents and immutable event research runs."""
from alembic import op
revision='20260914_0097'
down_revision='20260914_0096'
branch_labels=None
depends_on=None

def upgrade():
    op.execute('''CREATE TABLE quant.event_research_documents (
        document_id text PRIMARY KEY, content_hash text NOT NULL,
        published_at timestamptz NOT NULL, available_at timestamptz NOT NULL,
        document jsonb NOT NULL);
        CREATE INDEX event_research_time_idx ON quant.event_research_documents(available_at,published_at);
        CREATE TABLE quant.event_research_runs (
        run_id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        cutoff timestamptz NOT NULL, input_hash text NOT NULL, status text NOT NULL,
        result jsonb NOT NULL);
        CREATE INDEX event_research_runs_time_idx ON quant.event_research_runs(cutoff DESC)''')

def downgrade():
    op.execute('DROP TABLE quant.event_research_runs; DROP TABLE quant.event_research_documents')
