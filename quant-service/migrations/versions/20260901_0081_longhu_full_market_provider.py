"""Register the purchased Longhu/Tencent post-close composite provider.

Revision ID: 20260901_0081
Revises: 20260901_0080
"""

from alembic import op


revision = "20260901_0081"
down_revision = "20260901_0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO quant.providers(provider_key,label,enabled,config)
        VALUES (
          'longhuvip_composite','LonghuVIP industry cross-section + Tencent OHLC',true,
          jsonb_build_object(
            'secret_source','external_config_file','vendor_max_page_size',300,
            'flow_semantics','order_size_classified_not_institution_identity',
            'quote_crosscheck','tencent_batch')
        )
        ON CONFLICT(provider_key) DO UPDATE SET
          label=EXCLUDED.label,enabled=true,config=EXCLUDED.config,updated_at=now();

        INSERT INTO quant.provider_capabilities(
          provider_key,capability,market,priority,enabled,rate_limit_per_minute)
        VALUES
          ('longhuvip_composite','daily','cn',25,true,120),
          ('longhuvip_composite','daily_all_a','cn',25,true,120),
          ('longhuvip_composite','daily_basic','cn',25,true,120),
          ('longhuvip_composite','stock_money_flow','cn',25,true,120),
          ('longhuvip_composite','realtime_quote','cn',25,true,120)
        ON CONFLICT(provider_key,capability,market) DO UPDATE SET
          priority=EXCLUDED.priority,enabled=true,
          rate_limit_per_minute=EXCLUDED.rate_limit_per_minute;

        INSERT INTO quant.provider_api_capabilities(
          provider_key,api_name,availability,frequency,decision_eligible,note,verified_at,metadata)
        VALUES
          ('longhuvip_composite','daily','declared','post_close',false,
           'Coverage-gated purchased cross-section; decision eligibility is promoted only after a real successful run.',
           null,jsonb_build_object('physical_page_limit',300,'ohlc_crosscheck','tencent')),
          ('longhuvip_composite','stock_money_flow','declared','post_close',false,
           'Field 13 is vendor order-size-classified main net amount, not institution identity or Level-2.',
           null,'{"convention":"longhuvip_zs_stocklist_main_net_field13"}'::jsonb),
          ('longhuvip_composite','realtime_quote','declared','post_close',false,
           'Same-session Tencent quote cross-check; persisted as close snapshot evidence.',
           null,'{"crosscheck":"tencent_batch"}'::jsonb)
        ON CONFLICT(provider_key,api_name) DO UPDATE SET
          availability=EXCLUDED.availability,frequency=EXCLUDED.frequency,
          decision_eligible=EXCLUDED.decision_eligible,note=EXCLUDED.note,
          metadata=EXCLUDED.metadata,last_checked_at=now();
    """)


def downgrade() -> None:
    op.execute("DELETE FROM quant.providers WHERE provider_key='longhuvip_composite'")
