"""Normalize legacy non-finite public-event JSON literals.

Revision ID: 20260816_0036
Revises: 20260816_0035
"""

from alembic import op


revision = "20260816_0036"
down_revision = "20260816_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical Python JSON encoding admitted non-standard NaN/Infinity
    # tokens. AKShare normalization now converts non-finite values to null;
    # repair retained legacy text so downstream JSONB readers remain safe.
    op.execute(r"""
        UPDATE quant.market_events
           SET body = regexp_replace(
               regexp_replace(
                   regexp_replace(body,
                       '(:[[:space:]]*)-Infinity([[:space:]]*[,}])',
                       E'\\1null\\2', 'g'),
                   '(:[[:space:]]*)Infinity([[:space:]]*[,}])',
                   E'\\1null\\2', 'g'),
               '(:[[:space:]]*)NaN([[:space:]]*[,}])',
               E'\\1null\\2', 'g')
         WHERE body IS NOT JSON
           AND body ~ ':[[:space:]]*(-?Infinity|NaN)[[:space:]]*[,}]'
    """)


def downgrade() -> None:
    # Normalizing a non-finite value to JSON null is intentionally irreversible.
    pass
