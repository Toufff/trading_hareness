"""Record the pre-Alembic quant schema as an explicit baseline.

The frozen legacy DDL (``app.database.SCHEMA_SQL``) is applied by
``database_bootstrap.py`` and then stamped as this revision.  The schema and
the ``pgcrypto`` extension (``gen_random_uuid()`` defaults) are the only two
objects every later revision assumes, so they are created here idempotently:
an empty database can then run ``alembic upgrade head`` past ``0002`` instead
of failing with ``schema "quant" does not exist``.
"""

from alembic import op


revision = "20260811_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE SCHEMA IF NOT EXISTS quant")


def downgrade() -> None:
    # The baseline never drops the schema: it may still hold the frozen legacy
    # tables and the ``alembic_version`` table that Alembic itself writes.
    pass
