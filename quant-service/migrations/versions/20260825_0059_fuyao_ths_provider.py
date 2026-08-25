"""Register the documented Fuyao/THS realtime provider contract.

Revision ID: 20260825_0059
Revises: 20260824_0058
"""

from alembic import op


revision = "20260825_0059"
down_revision = "20260824_0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO quant.providers(provider_key,label,enabled)
        VALUES ('fuyao_ths','同花顺 Fuyao 数据服务',true)
        ON CONFLICT(provider_key) DO UPDATE SET label=EXCLUDED.label,enabled=true,updated_at=now();
        INSERT INTO quant.provider_capabilities(provider_key,capability,market,priority,enabled,rate_limit_per_minute)
        VALUES ('fuyao_ths','realtime_quote','cn',12,true,120)
        ON CONFLICT(provider_key,capability,market) DO UPDATE
          SET priority=EXCLUDED.priority,enabled=EXCLUDED.enabled,rate_limit_per_minute=EXCLUDED.rate_limit_per_minute;
        INSERT INTO quant.provider_api_capabilities(provider_key,api_name,availability,frequency,decision_eligible,note,verified_at,metadata)
        VALUES ('fuyao_ths','a_share_prices_snapshot','declared','realtime',false,
                'Fuyao documented all-A snapshot. It is research evidence until this deployment records a valid market-session observation.',
                null,'{"source":"official_docs"}'::jsonb)
        ON CONFLICT(provider_key,api_name) DO NOTHING;
    """)


def downgrade() -> None:
    op.execute("DELETE FROM quant.providers WHERE provider_key='fuyao_ths'")
